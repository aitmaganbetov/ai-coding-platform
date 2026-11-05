"""Networked preparation of offline tool data (ADR-0027): vulnerability databases and
toolchains, written to the platform cache by ``ai-coding tools pull``.

This is an explicit, user-initiated network action (like pulling images); scans then
run with ``--network none``. Preparation runs in the tool's own digest-pinned image
as the non-root sandbox user, with only the cache (and a seed directory) mounted.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ai_coding.config.models import DockerConfig
from ai_coding.executors import DockerSandboxExecutor
from ai_coding.executors.base import CommandSpec, Executor, Mount
from ai_coding.tools.cache import tool_cache

SANDBOX_CACHE = "/cache"
SANDBOX_TOOLCHAIN = "/toolchain"
SANDBOX_SEED = "/seed"


def cache_name(adapter: Any) -> str | None:
    """Platform cache entry an adapter needs, if any."""
    return getattr(adapter, "cache_name", None) or getattr(adapter, "toolchain_cache", None)


def needs_prepare(adapter: Any) -> bool:
    """Whether ``tools pull`` must prepare offline data for ``adapter``."""
    return hasattr(adapter, "prepare_args") or hasattr(adapter, "prepare")


def is_prepared(adapter: Any) -> bool:
    """Whether the offline data exists."""
    name = cache_name(adapter)
    if name is None:
        return True
    path = tool_cache(name)
    ready = getattr(adapter, "cache_ready", None)
    if ready is not None:
        return path.is_dir() and bool(ready(path))
    return path.is_dir() and any(path.iterdir())


def network_executor() -> DockerSandboxExecutor:
    """Executor allowed to use the network (preparation only)."""
    return DockerSandboxExecutor(docker_config=DockerConfig(network="bridge"))


async def prepare(adapter: Any, executor: Executor | None = None) -> None:
    """Download/install the offline data of ``adapter`` into the platform cache."""
    name = cache_name(adapter)
    if name is None or not needs_prepare(adapter):
        return
    executor = executor or network_executor()
    cache = tool_cache(name)
    cache.mkdir(parents=True, exist_ok=True)
    cache.chmod(0o777)  # written by the sandbox user (uid 1000)
    spec = adapter.spec
    with tempfile.TemporaryDirectory(prefix="ai-coding-seed-") as tmp:
        if hasattr(adapter, "prepare"):
            argv, env = adapter.prepare()
            mounts: tuple[Mount, ...] = (Mount(cache, SANDBOX_TOOLCHAIN, read_only=False),)
            entry: tuple[str, ...] = tuple(argv)
        else:
            mounts = (Mount(cache, SANDBOX_CACHE, read_only=False),)
            env = dict(getattr(adapter, "scan_env", lambda extra: {})({"cache": SANDBOX_CACHE}))
            seed_files = getattr(adapter, "seed_files", None)
            if seed_files:
                seed = Path(tmp)
                for rel, text in seed_files.items():
                    (seed / rel).write_text(text, encoding="utf-8")
                    (seed / rel).chmod(0o644)
                seed.chmod(0o755)
                mounts += (Mount(seed, SANDBOX_SEED, read_only=True),)
                entry = (spec.binary, *adapter.prepare_args(SANDBOX_CACHE, SANDBOX_SEED))
            else:
                entry = (spec.binary, *adapter.prepare_args(SANDBOX_CACHE))
        result = await executor.run(CommandSpec(
            argv=entry, cwd=cache, image=spec.image, mounts=mounts, network=True,
            env={**dict(spec.env), **dict(env)}, timeout_seconds=1800.0,
            ok_exit_codes=frozenset(range(256)),
        ))  # fmt: skip
    if not is_prepared(adapter):
        tail = (result.stderr or result.stdout).strip()[-400:]
        raise RuntimeError(f"{spec.name}: preparation failed (exit {result.exit_code}): {tail}")
