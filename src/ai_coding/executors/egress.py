"""Egress gateway for networked sandbox steps (ТЗ §12, B-205).

A step that needs the network (dependency installation, package upgrades) must reach
only the hosts its policy allows. ``docker run --network bridge`` would give it the
whole internet — a hostile install script could send the code anywhere. Instead, for
the duration of the step:

1. an ``--internal`` Docker network is created (no route outside);
2. the allowlist proxy (:mod:`ai_coding.executors.egress_proxy`, standard library only)
   runs in the digest-pinned Python runtime image as ``nobody``, read-only, without
   capabilities, attached to the default bridge *and* to the internal network;
3. the sandbox container joins only the internal network and gets the proxy in
   ``HTTPS_PROXY``/``HTTP_PROXY`` (npm, pip, composer, go and cargo honour them) and no
   DNS; anything that ignores the proxy has no route at all.

Afterwards both are removed; the proxy's decisions (JSON lines) explain refusals.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from ai_coding.executors.base import ExecutorError

# The Python runtime image (tools/testrun.PYTHON_IMAGE, pulled by `ai-coding tools pull`).
EGRESS_PROXY_IMAGE = (
    "mirror.gcr.io/library/python"
    "@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e"
)
PROXY_PORT = 3128
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "npm_config_proxy",
             "npm_config_https_proxy", "CARGO_HTTP_PROXY")  # fmt: skip
_SOURCE = Path(__file__).with_name("egress_proxy.py")


@dataclass
class Gateway:
    """A running egress gateway: ``route`` is ``(internal network, proxy URL)``."""

    network: str
    proxy: str
    route: tuple[str, str] = ("", "")
    decisions: list[dict[str, object]] = field(default_factory=list)

    def denied(self) -> list[str]:
        return [f"{d.get('target')} ({d.get('reason')})"
                for d in self.decisions if d.get("allowed") is False]  # fmt: skip


def _run(docker: str, env: Mapping[str, str], *args: str, timeout: float = 60) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False,
        env=dict(env),
    )  # fmt: skip
    if proc.returncode != 0:
        raise ExecutorError(f"egress gateway: docker {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def proxy_argv(name: str, allow: tuple[str, ...], *, allow_private: bool = False) -> list[str]:
    """``docker run`` argv of the proxy container (exposed for review and tests)."""
    argv = ["run", "-d", "--rm", "--name", name, "--pull", "never",
            "--network", "bridge", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--pids-limit", "128", "--memory", "128m", "--cpus", "0.5",
            "--label", "ai-coding.egress=proxy", "--entrypoint", "python3",
            EGRESS_PROXY_IMAGE, "-I", "-c", _SOURCE.read_text(encoding="utf-8"),
            "--port", str(PROXY_PORT)]  # fmt: skip
    for host in allow:
        if not host or any(c.isspace() for c in host) or host.startswith("-"):
            raise ExecutorError(f"invalid egress host {host!r}")
        argv += ["--allow", host]
    if allow_private:
        argv.append("--allow-private")
    return argv


@asynccontextmanager
async def egress_gateway(
    docker: str,
    name: str,
    allow: tuple[str, ...],
    env: Mapping[str, str],
    *,
    allow_private: bool = False,
) -> AsyncIterator[Gateway]:
    """Internal network + allowlist proxy for one sandbox step."""
    gateway = Gateway(network=f"{name}-net", proxy=f"{name}-egress")
    created: list[tuple[str, ...]] = []

    def call(*args: str) -> str:
        return _run(docker, env, *args)

    try:
        await asyncio.to_thread(call, "network", "create", "--internal",
                                "--label", "ai-coding.egress=network", gateway.network)  # fmt: skip
        created.append(("network", "rm", gateway.network))
        await asyncio.to_thread(call, *proxy_argv(gateway.proxy, allow,
                                                   allow_private=allow_private))  # fmt: skip
        created.append(("rm", "-f", gateway.proxy))
        await asyncio.to_thread(call, "network", "connect", gateway.network, gateway.proxy)
        address = await asyncio.to_thread(
            call, "inspect", "--format",
            f'{{{{(index .NetworkSettings.Networks "{gateway.network}").IPAddress}}}}',
            gateway.proxy,
        )  # fmt: skip
        await _wait_ready(call, gateway.proxy)
        gateway.route = (gateway.network, f"http://{address}:{PROXY_PORT}")
        yield gateway
    finally:
        if any(c[0] == "rm" for c in created):
            with suppress(ExecutorError, subprocess.TimeoutExpired):
                gateway.decisions = _decisions(await asyncio.to_thread(call, "logs", gateway.proxy))
        for args in reversed(created):  # proxy first, then the network
            with suppress(ExecutorError, subprocess.TimeoutExpired):
                await asyncio.to_thread(call, *args)


async def start_gateway(
    docker: str, name: str, allow: tuple[str, ...], env: Mapping[str, str]
) -> Gateway:
    """A long-lived gateway (a workspace terminal's) named after ``name``; removed by
    :func:`stop_gateway`, by name, from any process."""
    gateway = Gateway(network=f"{name}-net", proxy=f"{name}-egress")

    def call(*args: str) -> str:
        return _run(docker, env, *args)

    try:
        await asyncio.to_thread(call, "network", "create", "--internal",
                                "--label", "ai-coding.egress=network", gateway.network)  # fmt: skip
        await asyncio.to_thread(call, *proxy_argv(gateway.proxy, allow))
        await asyncio.to_thread(call, "network", "connect", gateway.network, gateway.proxy)
        address = await asyncio.to_thread(
            call, "inspect", "--format",
            f'{{{{(index .NetworkSettings.Networks "{gateway.network}").IPAddress}}}}',
            gateway.proxy,
        )  # fmt: skip
        await _wait_ready(call, gateway.proxy)
    except BaseException:
        await stop_gateway(docker, name, env)
        raise
    gateway.route = (gateway.network, f"http://{address}:{PROXY_PORT}")
    return gateway


async def stop_gateway(docker: str, name: str, env: Mapping[str, str]) -> None:
    """Remove the gateway of :func:`start_gateway` (missing parts are ignored)."""
    for args in (("rm", "-f", f"{name}-egress"), ("network", "rm", f"{name}-net")):
        with suppress(ExecutorError, subprocess.TimeoutExpired):
            await asyncio.to_thread(_run, docker, env, *args)


async def _wait_ready(call: Callable[..., str], proxy: str, timeout: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        logs = await asyncio.to_thread(call, "logs", proxy)
        if '"ready": true' in logs:
            return
        await asyncio.sleep(0.1)
    raise ExecutorError("egress gateway: the proxy did not start")


def _decisions(logs: str) -> list[dict[str, object]]:
    out = []
    for line in logs.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("event") == "egress" and "allowed" in entry:
            out.append(entry)
    return out
