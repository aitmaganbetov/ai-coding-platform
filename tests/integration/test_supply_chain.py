"""B-802: SBOMs of a release and the guarantees of the release workflow."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_coding import __version__
from tests.integration.conftest import ALPINE, require_images

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sbom.py"


def _sbom(tmp_path: Path, *args: str) -> None:
    subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path), *args], check=True,
                   capture_output=True, cwd=ROOT)  # fmt: skip


def _load(path: Path) -> dict[str, Any]:
    bom = json.loads(path.read_text(encoding="utf-8"))
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"].startswith("1.")
    return bom  # type: ignore[no-any-return]


def _locked_runtime() -> dict[str, str]:
    exported = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project",
         "--extra", "server", "--extra", "postgres", "--format", "requirements-txt"],
        check=True, capture_output=True, text=True, cwd=ROOT,
    ).stdout  # fmt: skip
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", exported, re.M)
    return {name.lower().replace("_", "-"): version for name, version in pins}


def test_python_sbom_is_the_locked_runtime(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    _sbom(tmp_path, "--no-web")
    bom = _load(tmp_path / "sbom-python.cdx.json")
    root = bom["metadata"]["component"]
    assert root["name"] == "ai-coding-platform" and root["version"] == __version__
    found = {c["name"].lower().replace("_", "-"): c["version"] for c in bom["components"]}
    assert found == _locked_runtime()  # exactly uv.lock's runtime set, same versions
    assert {"fastapi", "pydantic", "sqlalchemy", "psycopg"} <= set(found)
    assert not {"pytest", "ruff", "mypy", "schemathesis"} & set(found)  # no dev tools
    assert all(c.get("purl", "").startswith("pkg:pypi/") for c in bom["components"])


def test_web_sbom_is_what_the_bundle_ships(tmp_path: Path) -> None:
    if shutil.which("npm") is None or shutil.which("uv") is None:
        pytest.skip("npm and uv are needed")
    _sbom(tmp_path)
    bom = _load(tmp_path / "sbom-web.cdx.json")
    names = {c["name"]: c["version"] for c in bom["components"]}
    assert names["react"] == names["react-dom"] == "18.3.1"
    assert not {"vite", "vitest", "typescript", "jsdom"} & set(names)  # build/test only
    refs = {c["bom-ref"] for c in bom["components"]} | {bom["metadata"]["component"]["bom-ref"]}
    assert all(d["ref"] in refs and set(d.get("dependsOn", [])) <= refs
               for d in bom["dependencies"])  # fmt: skip


@pytest.mark.docker
def test_image_sbom_without_socket_or_network(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    from sbom import SYFT_IMAGE  # noqa: PLC0415

    require_images(ALPINE, SYFT_IMAGE)
    _sbom(tmp_path, "--no-web", "--image", ALPINE)
    bom = _load(tmp_path / "sbom-image.cdx.json")
    packages = {c["name"] for c in bom["components"] if c.get("type") == "library"}
    assert {"busybox", "musl", "apk-tools"} <= packages


def _workflow() -> dict[str, Any]:
    text = (ROOT / ".github" / "workflows" / "release-platform.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)  # type: ignore[no-any-return]


def test_release_workflow_guarantees() -> None:
    wf = _workflow()
    trigger = wf.get("on", wf.get(True))
    assert trigger == {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}}
    assert wf["permissions"] == {}  # nothing by default; each job asks for its own
    jobs = wf["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    signers = {name for name, job in jobs.items() if job["permissions"].get("id-token")}
    assert signers == {"image", "release", "pypi"}  # OIDC only where Sigstore/PyPI need it
    assert jobs["release"]["environment"] == "release"  # a person approves publication
    assert jobs["pypi"]["environment"] == "pypi"
    assert jobs["pypi"]["if"] == "vars.PYPI_PUBLISH == 'true'"
    steps = [s for job in jobs.values() for s in job["steps"]]
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    uses = " ".join(s.get("uses", "") for s in steps)
    for action in ("attest-build-provenance", "attest-sbom", "cosign-installer"):
        assert action in uses
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "cosign sign --yes" in runs and "cosign sign-blob --yes --bundle" in runs
    assert "--draft" in runs  # the release is published by a maintainer, not by CI
    assert "sha256sum --check --strict SHA256SUMS" in runs  # artifacts unchanged in transit
    assert 'test "v$version" = "$TAG"' in runs  # the tag is the package version
