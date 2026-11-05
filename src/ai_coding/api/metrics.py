"""Prometheus metrics in the text exposition format (NFR-07, B-707).

No client library: counters and a latency histogram for HTTP requests, plus gauges
computed from the database at scrape time (runs by state, queued/leased jobs) and
the worker's active-run gauge. Labels never contain user data: the route template
(``/api/v1/runs/{run_id}``), not the path, identifies a request.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ai_coding.persistence.models import Run, RunJob

if TYPE_CHECKING:
    from ai_coding.api.services import Server

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    """Process-local request metrics plus database gauges."""

    def __init__(self, server: Server | None = None) -> None:
        self._server = server
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._hist: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * len(BUCKETS))
        self._sum: dict[tuple[str, str], float] = defaultdict(float)
        self._count: dict[tuple[str, str], int] = defaultdict(int)
        self.active_runs = 0
        self.jobs_done = 0
        self.jobs_failed = 0

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        key = (method, route)
        with self._lock:
            self._requests[(method, route, str(status))] += 1
            self._sum[key] += seconds
            self._count[key] += 1
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    self._hist[key][i] += 1

    def render(self) -> str:
        """The exposition text."""
        lines = [
            "# HELP aicoding_http_requests_total HTTP requests by method, route and status.",
            "# TYPE aicoding_http_requests_total counter",
        ]
        with self._lock:
            for (method, route, status), n in sorted(self._requests.items()):
                lines.append(
                    f'aicoding_http_requests_total{{method="{method}",route="{_esc(route)}",'
                    f'status="{status}"}} {n}'
                )
            lines += [
                "# HELP aicoding_http_request_duration_seconds Request latency.",
                "# TYPE aicoding_http_request_duration_seconds histogram",
            ]
            for (method, route), counts in sorted(self._hist.items()):
                labels = f'method="{method}",route="{_esc(route)}"'
                name = "aicoding_http_request_duration_seconds_bucket"
                for bound, n in zip(BUCKETS, counts, strict=True):
                    lines.append(f'{name}{{{labels},le="{bound}"}} {n}')
                total = self._count[(method, route)]
                lines += [
                    f'aicoding_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {total}',
                    f"aicoding_http_request_duration_seconds_sum{{{labels}}} "
                    f"{self._sum[(method, route)]:.6f}",
                    f"aicoding_http_request_duration_seconds_count{{{labels}}} {total}",
                ]
            lines += [
                "# HELP aicoding_worker_active_runs Runs executing in this process.",
                "# TYPE aicoding_worker_active_runs gauge",
                f"aicoding_worker_active_runs {self.active_runs}",
                "# HELP aicoding_worker_jobs_total Jobs finished by this process.",
                "# TYPE aicoding_worker_jobs_total counter",
                f'aicoding_worker_jobs_total{{outcome="done"}} {self.jobs_done}',
                f'aicoding_worker_jobs_total{{outcome="failed"}} {self.jobs_failed}',
            ]
        lines += self._db_gauges()
        return "\n".join(lines) + "\n"

    def _db_gauges(self) -> list[str]:
        if self._server is None:
            return []
        with self._server.session() as s:
            runs = s.execute(select(Run.state, func.count()).group_by(Run.state)).all()
            jobs = s.execute(select(RunJob.state, func.count()).group_by(RunJob.state)).all()
        lines = ["# HELP aicoding_runs Runs by state.", "# TYPE aicoding_runs gauge"]
        lines += [f'aicoding_runs{{state="{state}"}} {n}' for state, n in sorted(runs)]
        lines += ["# HELP aicoding_jobs Queue entries by state.", "# TYPE aicoding_jobs gauge"]
        lines += [f'aicoding_jobs{{state="{state}"}} {n}' for state, n in sorted(jobs)]
        return lines


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
