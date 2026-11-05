"""Load test harness (B-800, NFR-02): a production-shaped server under k6.

Starts PostgreSQL (digest-pinned, throwaway) unless ``--database-url`` is given, the API
(``ai-coding server serve``) and ``--workers`` separate worker processes, registers a
project, then runs ``loadtest/server.js`` in the digest-pinned k6 image. The exit code is
k6's: 0 when every threshold holds, 99 when one is crossed.

    uv run python loadtest/run_load.py --out load-out

Outputs: ``k6-summary.json`` (k6), ``summary.md`` (people), ``server.log``, ``worker-*.log``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
K6_IMAGE = (
    "mirror.gcr.io/grafana/k6"
    "@sha256:3ddc8b1a33a2c3d8edc6e99b6a762ae36cba08788463458f5e6a7703e14eb77d"
)  # v1.3.0
POSTGRES_IMAGE = (
    "mirror.gcr.io/library/postgres"
    "@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b"
)  # 17-alpine
PASSWORD = secrets.token_urlsafe(18)


def _docker() -> str:
    path = shutil.which("docker")
    if path is None:
        raise SystemExit("docker is required (PostgreSQL and k6 run in containers)")
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def postgres() -> Iterator[str]:
    """A throwaway PostgreSQL; yields its SQLAlchemy URL."""
    secret, name = secrets.token_hex(16), f"aic-load-pg-{secrets.token_hex(4)}"
    subprocess.run(
        [
            _docker(),
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={secret}",
            "-p",
            "127.0.0.1::5432",
            POSTGRES_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(
                [_docker(), "port", name, "5432/tcp"], check=True, capture_output=True, text=True
            )
            .stdout.split(":")[-1]
            .strip()
        )
        deadline = time.monotonic() + 60
        while (
            subprocess.run(
                [_docker(), "exec", name, "pg_isready", "-U", "postgres"],
                capture_output=True,
                check=False,
            ).returncode
            != 0
        ):
            if time.monotonic() > deadline:
                raise RuntimeError("PostgreSQL did not start")
            time.sleep(0.5)
        time.sleep(1.0)  # the init process restarts the server once after pg_isready
        yield f"postgresql+psycopg://postgres:{secret}@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([_docker(), "rm", "-f", name], capture_output=True, check=False)


def _cli(env: dict[str, str], *args: str, stdin: str | None = None) -> None:
    subprocess.run(
        [sys.executable, "-m", "ai_coding", "server", *args],
        env=env,
        input=stdin,
        text=True,
        check=True,
        capture_output=True,
    )


def _wait(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with suppress(httpx.HTTPError):
            if httpx.get(f"{url}/readyz", timeout=2).status_code == 200:
                return
        time.sleep(0.3)
    raise RuntimeError(f"{url} did not become ready")


def _project(root: Path) -> Path:
    repo = root / "repos" / "sample"
    (repo / "src").mkdir(parents=True)
    for i in range(20):
        (repo / "src" / f"module_{i}.py").write_text(
            f"def f{i}(x):\n    return x + {i}\n", encoding="utf-8"
        )
    (repo / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    return repo


def summarize(summary: dict[str, Any], exit_code: int, setup: dict[str, Any]) -> str:
    """Markdown table of the key results."""
    m = summary["metrics"]

    def val(metric: str, stat: str) -> str:
        data = m.get(metric, {})
        value = data.get(stat, data.get("values", {}).get(stat))
        return "—" if value is None else f"{value:.1f}" if isinstance(value, float) else str(value)

    reads = next((k for k in m if k.startswith("http_req_duration{scenario:reads}")), "")
    failed = next((k for k in m if k.startswith("http_req_failed{scenario:reads}")), "")
    rows = [
        ("Результат (все пороги)", "✅ пройдены" if exit_code == 0 else f"❌ k6 exit {exit_code}"),
        ("Конфигурация", ", ".join(f"{k}={v}" for k, v in setup.items())),
        ("Запросов всего / в секунду", f"{val('http_reqs', 'count')} / {val('http_reqs', 'rate')}"),
        (
            "Чтение API: p95 / p99 / max, мс",
            f"{val(reads, 'p(95)')} / {val(reads, 'p(99)')} / {val(reads, 'max')}",
        ),
        ("Чтение API: доля ошибок", val(failed, "value")),
        ("Прогонов завершено", val("runs_completed", "count")),
        (
            "Длительность прогона: med / p95 / max, мс",
            f"{val('run_duration', 'med')} / {val('run_duration', 'p(95)')} / "
            f"{val('run_duration', 'max')}",
        ),
        ("Одновременно выполнявшихся прогонов (max)", val("parallel_runs", "max")),
        ("Неуспешных прогонов (доля)", val("runs_failed", "value")),
    ]
    return (
        "\n".join(
            ["| Показатель | Значение |", "| --- | --- |", *(f"| {k} | {v} |" for k, v in rows)]
        )
        + "\n"
    )


def main() -> int:  # noqa: PLR0915 - one linear orchestration of processes
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=Path("load-out"))
    ap.add_argument("--database-url", help="Use this database instead of a throwaway PostgreSQL")
    ap.add_argument("--api-workers", type=int, default=2, help="API processes (serve --workers)")
    ap.add_argument("--workers", type=int, default=2, help="Worker processes")
    ap.add_argument("--concurrency", type=int, default=10, help="Runs per worker process")
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--run-vus", type=int, default=30)
    ap.add_argument("--read-rate", type=int, default=15, help="Read iterations/s (3 requests)")
    ap.add_argument("--duration", default="60s")
    ap.add_argument("--min-parallel", type=int, default=10)
    ap.add_argument("--p95-ms", type=int, default=500)
    ap.add_argument("--poll-seconds", type=float, default=1.0)
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o777)  # k6 runs as an unprivileged user in its container
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="aic-load-") as tmp, contextlib_db(args) as db_url:
        root = Path(tmp)
        repo = _project(root)
        metrics_token = secrets.token_urlsafe(24)
        env = {
            **os.environ,
            "AI_CODING_SERVER_DATA_DIR": str(root / "data"),
            "AI_CODING_SERVER_DATABASE_URL": db_url,
            "AI_CODING_SERVER_ALLOWED_ROOTS": str(repo.parent),
            "AI_CODING_SERVER_WORKERS": str(args.concurrency),
            "AI_CODING_SERVER_RATE_LIMIT": "100000",
            "AI_CODING_SERVER_METRICS_TOKEN": metrics_token,
        }
        _cli(env, "migrate")
        _cli(env, "user", "add", "loadadmin", "--admin", "--password-stdin", stdin=PASSWORD + "\n")
        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        try:
            serve = ["serve", "--port", str(port), "--workers", str(args.api_workers)]
            for name, argv in [
                ("server", serve),
                *((f"worker-{i}", ["worker"]) for i in range(args.workers)),
            ]:
                log = (out / f"{name}.log").open("w", encoding="utf-8")
                logs.append(log)
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "ai_coding", "server", *argv],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                )
            _wait(url)
            with httpx.Client(base_url=url, timeout=30) as c:
                token = c.post(
                    "/api/v1/auth/login", json={"username": "loadadmin", "password": PASSWORD}
                ).json()["token"]
                h = {"Authorization": f"Bearer {token}"}
                project = c.post(
                    "/api/v1/projects", headers=h, json={"name": "sample", "path": str(repo)}
                )
                project.raise_for_status()
            setup = {
                "api_workers": args.api_workers,
                "workers": args.workers,
                "concurrency": args.concurrency,
                "runs": args.runs,
                "run_vus": args.run_vus,
                "read_rate": args.read_rate,
                "duration": args.duration,
                "db": db_url.split(":", 1)[0],
            }
            k6_env = {
                "BASE_URL": url,
                "TOKEN": token,
                "PROJECT_ID": project.json()["id"],
                "METRICS_TOKEN": metrics_token,
                "RUNS": args.runs,
                "RUN_VUS": args.run_vus,
                "READ_RATE": args.read_rate,
                "DURATION": args.duration,
                "MIN_PARALLEL": args.min_parallel,
                "P95_MS": args.p95_ms,
                "POLL_SECONDS": args.poll_seconds,
            }
            argv = [
                _docker(),
                "run",
                "--rm",
                "--network",
                "host",
                "-v",
                f"{HERE}:/scripts:ro",
                "-v",
                f"{out}:/out",
            ]
            for key, value in k6_env.items():
                argv += ["-e", f"{key}={value}"]
            argv += [
                K6_IMAGE,
                "run",
                "--quiet",
                "--summary-export",
                "/out/k6-summary.json",
                "/scripts/server.js",
            ]
            code = subprocess.run(argv, check=False).returncode
        finally:
            for proc in processes:
                proc.terminate()
            for proc in processes:
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=60)
                if proc.poll() is None:
                    proc.kill()
            for log in logs:
                log.close()
    summary_path = out / "k6-summary.json"
    if not summary_path.is_file():
        print(f"k6 produced no summary (exit {code})", file=sys.stderr)
        return code or 1
    table = summarize(json.loads(summary_path.read_text(encoding="utf-8")), code, setup)
    (out / "summary.md").write_text("# Нагрузочный тест (k6)\n\n" + table, encoding="utf-8")
    print(table)
    return code


@contextmanager
def contextlib_db(args: argparse.Namespace) -> Iterator[str]:
    if args.database_url:
        yield args.database_url
    else:
        _docker()
        with postgres() as url:
            yield url


if __name__ == "__main__":
    raise SystemExit(main())
