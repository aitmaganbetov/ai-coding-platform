"""B-205: a networked sandbox step reaches only its allowlisted hosts.

The step runs on an internal Docker network whose only way out is the allowlist proxy;
probes from inside try the allowed and forbidden ways out.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from ai_coding.config.models import DockerConfig
from ai_coding.executors import CommandSpec, DockerSandboxExecutor
from ai_coding.executors.egress import EGRESS_PROXY_IMAGE, proxy_argv
from ai_coding.tools.testrun import PYTHON_IMAGE
from tests.integration.conftest import require_images

pytestmark = [pytest.mark.integration, pytest.mark.docker]

PROBE = r"""
import socket, urllib.request, urllib.error
def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return f"OK {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except OSError as e:
        text = str(e)
        return "DENIED" if "403" in text else f"ERROR {type(e).__name__}"
def direct(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "CONNECTED"
    except OSError:
        return "NO-ROUTE"
def dns(name):
    try:
        socket.getaddrinfo(name, 443)
        return "RESOLVED"
    except OSError:
        return "NO-DNS"
print("forbidden=" + fetch("https://example.com/"))
print("ssrf=" + fetch("https://localhost./"))  # not NO_PROXY: through the proxy
print("direct=" + direct("1.1.1.1", 443))
print("dns=" + dns("example.com"))
print("allowed=" + fetch("https://pypi.org/simple/pip/"))
"""


def _leftovers() -> str:
    nets = subprocess.run(
        ["docker", "network", "ls", "-q", "--filter", "label=ai-coding.egress"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    boxes = subprocess.run(["docker", "ps", "-aq", "--filter", "label=ai-coding.egress"],
                           capture_output=True, text=True, check=False).stdout  # fmt: skip
    return (nets + boxes).strip()


def _internet() -> bool:
    """Whether containers on the default bridge can fetch from the internet over verified
    HTTPS here (not behind a TLS-intercepting proxy, as in some development sandboxes)."""
    probe = subprocess.run(
        ["docker", "run", "--rm", "--network", "bridge", PYTHON_IMAGE, "python3", "-c",
         "import urllib.request; urllib.request.urlopen('https://pypi.org/simple/pip/', "
         "timeout=15)"],
        capture_output=True, check=False,
    )  # fmt: skip
    return probe.returncode == 0


def test_proxy_image_is_the_pulled_python_runtime() -> None:
    assert EGRESS_PROXY_IMAGE == PYTHON_IMAGE  # pulled by `ai-coding tools pull`
    argv = proxy_argv("p", ("pypi.org", "*.npmjs.org"))
    for flag in ("--read-only", "no-new-privileges", "65534:65534", "--pids-limit"):
        assert flag in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[-4:] == ["--allow", "pypi.org", "--allow", "*.npmjs.org"]
    with pytest.raises(Exception, match="invalid egress host"):
        proxy_argv("p", ("--allow-everything",))


def test_networked_step_reaches_only_allowed_hosts(tmp_path: Path) -> None:
    require_images(PYTHON_IMAGE)
    spec = CommandSpec(argv=("python3", "-c", PROBE), cwd=tmp_path, image=PYTHON_IMAGE,
                       network=True, egress=("pypi.org", "files.pythonhosted.org", "localhost"),
                       timeout_seconds=120)  # fmt: skip
    sandbox = DockerSandboxExecutor(
        docker_config=DockerConfig(network="bridge")
    )  # policy allows it
    result = asyncio.run(sandbox.run(spec))
    assert result.exit_code == 0, result.stderr
    seen = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert seen["forbidden"] == "DENIED"  # not on the list: the proxy refuses
    assert seen["ssrf"] == "DENIED"  # allowed name, private address: refused
    assert seen["direct"] == "NO-ROUTE" and seen["dns"] == "NO-DNS"  # no way around
    assert "egress denied by policy: example.com:443" in result.stderr
    assert "localhost.:443 (resolves to non-public" in result.stderr
    assert _leftovers() == ""  # the network and the proxy are gone
    if not _internet():
        pytest.skip("no verified HTTPS to the internet from containers here (CI checks it)")
    assert seen["allowed"].startswith("OK"), seen  # the allowlisted registry works
