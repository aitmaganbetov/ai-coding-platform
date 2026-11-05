"""Load test harness (B-800): summary of the k6 results and pool sizing settings."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from ai_coding.api.settings import ServerSettings
from ai_coding.persistence import Database

ROOT = Path(__file__).resolve().parents[2]


def _harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_load", ROOT / "loadtest" / "run_load.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_table_reports_the_acceptance_numbers() -> None:
    summary = {"metrics": {
        "http_reqs": {"count": 3268, "rate": 54.3},
        "http_req_duration{scenario:reads}": {"p(95)": 371.4, "p(99)": 580.7, "max": 866.4},
        "http_req_failed{scenario:reads}": {"value": 0},
        "runs_completed": {"count": 60},
        "run_duration": {"med": 7390.5, "p(95)": 11113.6, "max": 12185},
        "parallel_runs": {"max": 20},
        "runs_failed": {"value": 0},
    }}  # fmt: skip
    table = _harness().summarize(summary, 0, {"workers": 2})
    assert "✅ пройдены" in table and "371.4 / 580.7 / 866.4" in table
    assert "| Одновременно выполнявшихся прогонов (max) | 20 |" in table
    assert "❌ k6 exit 99" in _harness().summarize({"metrics": {}}, 99, {})


def test_k6_script_encodes_the_thresholds() -> None:
    script = (ROOT / "loadtest" / "server.js").read_text(encoding="utf-8")
    for threshold in ("parallel_runs: [`max>=${MIN_PARALLEL}`]", "runs_failed: [\"rate==0\"]",
                      "`p(95)<${P95_MS}`", "rate<0.01"):  # fmt: skip
        assert threshold in script
    assert 'aicoding_jobs\\{state="leased"\\}' in script  # executing now, all workers
    assert "@sha256:" in _harness().K6_IMAGE  # digest-pinned


def test_database_pool_is_sized_from_the_settings(tmp_path: Path) -> None:
    env = {
        "AI_CODING_SERVER_DATA_DIR": str(tmp_path),
        "AI_CODING_SERVER_DB_POOL_SIZE": "7",
        "AI_CODING_SERVER_DB_MAX_OVERFLOW": "3",
    }
    settings = ServerSettings.from_env(env)
    assert (settings.db_pool_size, settings.db_max_overflow) == (7, 3)
    db = Database("postgresql+psycopg://u:p@127.0.0.1:1/db", pool_size=7, max_overflow=3)
    try:
        assert db.engine.pool.size() == 7  # type: ignore[attr-defined]
        assert db.engine.pool._max_overflow == 3  # type: ignore[attr-defined]
    finally:
        db.dispose()
