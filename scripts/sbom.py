"""Software bills of materials of a release (B-802, ТЗ §12), CycloneDX JSON.

    uv run python scripts/sbom.py --out dist/sbom [--image ai-coding-platform:2.1.0]

* ``sbom-python.cdx.json`` — the package and its runtime dependencies exactly as locked
  in ``uv.lock`` (with the ``server`` and ``postgres`` extras; no dev tools);
* ``sbom-web.cdx.json`` — the Web UI's production npm dependencies (``package-lock.json``);
* ``sbom-image.cdx.json`` — with ``--image``: every package of the server image, read by
  Syft (digest-pinned) from ``docker save`` output in a container without network — Syft
  never gets the Docker socket.

The release workflow attests each SBOM with Sigstore (``actions/attest-sbom``).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYFT_IMAGE = (
    "mirror.gcr.io/anchore/syft"
    "@sha256:f94e5d9fce1f2278491a8e3a63bd5f6ddb81fdfdbb8bf7a1637565c1d5344357"
)  # v1.33.0


def _run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, text=True, capture_output=True, **kw)  # type: ignore[call-overload,no-any-return]  # noqa: S603


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"{name} is required")
    return path


def python_sbom(out: Path) -> Path:
    """Runtime dependencies from uv.lock, the package itself as the root component."""
    target = out / "sbom-python.cdx.json"
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        exported = _run([_tool("uv"), "export", "--frozen", "--no-dev", "--no-hashes",
                         "--no-emit-project", "--extra", "server", "--extra", "postgres",
                         "--format", "requirements-txt"], cwd=ROOT).stdout  # fmt: skip
        requirements.write_text(exported, encoding="utf-8")
        _run([sys.executable, "-m", "cyclonedx_py", "requirements", str(requirements),
              "--pyproject", str(ROOT / "pyproject.toml"), "--output-format", "JSON",
              "--output-file", str(target)])  # fmt: skip
    _set_root_version(target)
    return target


def _set_root_version(path: Path) -> None:
    """``pyproject.toml`` has a dynamic version: take it from the package."""
    sys.path.insert(0, str(ROOT / "src"))
    from ai_coding import __version__  # noqa: PLC0415

    bom = json.loads(path.read_text(encoding="utf-8"))
    component = bom.setdefault("metadata", {}).setdefault("component", {})
    component["version"] = __version__
    component["purl"] = f"pkg:pypi/ai-coding-platform@{__version__}"
    path.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")


def web_sbom(out: Path) -> Path:
    """Dependencies shipped in the Web UI bundle, from its lock file.

    ``npm sbom --omit dev`` (npm 10.9) returns no components at all, so the full SBOM
    is filtered with the lock file's own ``dev``/``devOptional`` flags.
    """
    target = out / "sbom-web.cdx.json"
    web = ROOT / "web"
    result = _run([_tool("npm"), "sbom", "--sbom-format", "cyclonedx", "--package-lock-only"],
                  cwd=web)  # fmt: skip
    bom = json.loads(result.stdout)
    lock = json.loads((web / "package-lock.json").read_text(encoding="utf-8"))
    shipped = {
        (path.rsplit("node_modules/", 1)[-1], meta.get("version"))
        for path, meta in lock.get("packages", {}).items()
        if path and not meta.get("dev") and not meta.get("devOptional")
    }
    kept = [c for c in bom.get("components", [])
            if (_npm_name(c), c.get("version")) in shipped]  # fmt: skip
    refs = {c.get("bom-ref") for c in kept} | {bom.get("metadata", {}).get("component", {})
                                                .get("bom-ref")}  # fmt: skip
    bom["components"] = kept
    bom["dependencies"] = [
        {**d, "dependsOn": [r for r in d.get("dependsOn", []) if r in refs]}
        for d in bom.get("dependencies", [])
        if d.get("ref") in refs
    ]
    target.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
    return target


def _npm_name(component: dict[str, object]) -> str:
    group = component.get("group")
    return f"{group}/{component['name']}" if group else str(component["name"])


def image_sbom(out: Path, image: str) -> Path:
    """Packages of a local image via ``docker save`` and Syft (no socket, no network)."""
    docker = _tool("docker")
    target = out / "sbom-image.cdx.json"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "image.tar"
        _run([docker, "save", "--output", str(archive), image])
        archive.chmod(0o644)
        Path(tmp).chmod(0o755)
        result = _run([docker, "run", "--rm", "--network", "none", "--read-only",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--tmpfs", "/tmp:rw,size=4g", "-e", "XDG_CACHE_HOME=/tmp",  # noqa: S108 - container path
                       "-e", "SYFT_CHECK_FOR_APP_UPDATE=false",
                       "-v", f"{tmp}:/in:ro", SYFT_IMAGE, "docker-archive:/in/image.tar",
                       "-o", "cyclonedx-json", "-q"])  # fmt: skip
    target.write_text(result.stdout, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "sbom")
    parser.add_argument("--image", help="Also describe this local Docker image")
    parser.add_argument("--no-web", action="store_true", help="Skip the Web UI SBOM")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    written = [python_sbom(args.out)]
    if not args.no_web:
        written.append(web_sbom(args.out))
    if args.image:
        written.append(image_sbom(args.out, args.image))
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
