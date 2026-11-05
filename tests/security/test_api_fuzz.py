"""B-801 (DAST, input fuzzing): Schemathesis generates requests from the OpenAPI contract
— valid, boundary and malformed — with a valid token, against the real application.

Every response must be a client or success status (never 5xx), carry the security
headers, and errors must use the uniform JSON envelope without stack traces.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import schemathesis
from hypothesis import HealthCheck, given, settings
from hypothesis.errors import Unsatisfiable
from schemathesis import GenerationMode
from schemathesis.specs.openapi.schemas import OpenApiSchema

from ai_coding.api.app import create_app
from ai_coding.api.services import Server
from ai_coding.api.settings import ServerSettings

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"
SECURITY_HEADERS = ("x-content-type-options", "x-frame-options", "content-security-policy")


def _server(root: Path) -> tuple[Server, str]:
    repos = root / "repos"
    (repos / "p").mkdir(parents=True)
    (repos / "p" / "a.py").write_text("x = 1\n", encoding="utf-8")
    srv = Server(ServerSettings(data_dir=root / "data", database_url=f"sqlite:///{root / 's.db'}",
                                allowed_roots=(repos.resolve(),), rate_limit_per_minute=100_000,
                                login_rate_limit_per_minute=10_000))  # fmt: skip
    srv.migrate()
    srv.create_user("admin", PASSWORD, is_admin=True)
    admin = srv.login("admin", PASSWORD)
    assert admin is not None
    srv.create_project(admin, "p", path=str(repos.resolve() / "p"))
    token, _ = srv.issue_token(admin, "fuzz")
    return srv, token


@pytest.fixture(scope="module")
def target(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[OpenApiSchema, str]]:
    srv, token = _server(tmp_path_factory.mktemp("fuzz"))
    schema = schemathesis.openapi.from_asgi("/api/v1/openapi.json", create_app(srv))
    yield schema, token
    srv.close()


def _check(response: Any) -> None:
    assert response.status_code < 500, (response.status_code, response.text[:500])
    headers = {k.lower() for k in response.headers}
    for name in SECURITY_HEADERS:
        assert name in headers, name
    if response.status_code >= 400:
        error = response.json()["error"]
        assert set(error) >= {"code", "message"}
        assert "Traceback" not in response.text and 'File "' not in response.text


@pytest.mark.parametrize("mode", list(GenerationMode), ids=lambda m: m.value)
def test_fuzz_every_operation(target: tuple[OpenApiSchema, str], mode: GenerationMode) -> None:
    """Positive (schema-valid) and negative (malformed) requests for every operation."""
    schema, token = target
    operations = [op.ok() for op in schema.get_all_operations()]
    sent = 0
    assert len(operations) >= 20
    for operation in operations:

        @settings(max_examples=25, deadline=None, database=None,
                  suppress_health_check=list(HealthCheck))  # fmt: skip
        @given(case=operation.as_strategy(generation_mode=mode))
        def run(case: Any) -> None:
            nonlocal sent
            response = case.call(headers={"Authorization": f"Bearer {token}"})
            sent += 1
            _check(response)

        try:
            run()
        except Unsatisfiable:  # nothing can be negated in an operation without inputs
            assert mode is GenerationMode.NEGATIVE
    assert sent >= 10 * len(operations) // 2


def test_framework_errors_use_the_api_envelope(target: tuple[OpenApiSchema, str]) -> None:
    """Regression (found by the negative fuzzing): a malformed JSON body, an unknown route
    and a wrong method answered FastAPI's ``{"detail": ...}`` instead of the envelope."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    schema, token = target
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with TestClient(schema.app) as c:
        project = c.get("/api/v1/projects", headers=h).json()[0]["id"]
        cases = [
            c.post(f"/api/v1/projects/{project}/runs", headers=h, content=b'{"kind": '),
            c.get("/api/v1/no-such-route", headers=h),
            c.patch("/api/v1/me", headers=h),
        ]
    assert [r.status_code for r in cases] == [422, 404, 405]
    for r in cases:
        _check(r)
    assert cases[2].headers["allow"]
