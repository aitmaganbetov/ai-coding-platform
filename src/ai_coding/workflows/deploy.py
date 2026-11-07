"""Deployment runs (ADR-0048): a ref to an environment, through the orchestrator.

Steps of a ``deploy`` run:

1. ``deploy.resolve`` — the deployment and its environment; the ref → a commit (GitHub
   for a GitHub Actions environment, the project repository otherwise);
2. ``deploy.gates`` — when the environment requires it, the latest completed audit of the
   project must have no failed blocking Quality Gate;
3. ``deploy.approval`` — production environments, and those that require it, wait for a
   person's approval (operation ``deploy``, a mandatory approval of the platform: the
   run is AWAITING_APPROVAL until someone with ``run.approve`` decides);
4. ``deploy.execute`` — GitHub Actions: ``workflow_dispatch`` of the environment's
   workflow, followed to its conclusion (with a timeout); webhook: a POST signed with
   the environment's secret (``X-AI-Coding-Signature: sha256=<HMAC-SHA256 of the body>``),
   a 2xx answer accepts the deployment;
5. ``deploy.verify`` — the environment's health URL must answer 2xx;
6. ``report.deploy`` — a report artifact.

The steps reach the server's data (secrets, the deployment row, the latest gates) through
a :class:`DeployPort` the server registers in its process. Webhook and health URLs are
https only and are called through :class:`PublicOnlyTransport`: the address is resolved,
checked (no loopback, private, link-local, reserved or multicast address) and connected to
for each request, so a name cannot point the server at its own network (SSRF, DNS
rebinding).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from ai_coding.domain.enums import ApprovalOperation, TaskPhase
from ai_coding.domain.errors import PermanentError, TransientError
from ai_coding.git import LocalGitRepository
from ai_coding.git.errors import GitError
from ai_coding.integrations.github.client import GitHubAPIError, RateLimitError
from ai_coding.integrations.github.host import GitHubHost
from ai_coding.orchestrator.planner import StepSpec
from ai_coding.orchestrator.policy import Decision, Verdict
from ai_coding.orchestrator.registry import HandlerRegistry, StepContext, StepResult

POLL_SECONDS = 10.0  # GitHub Actions and health checks (tests shorten it)
FIND_SECONDS = 120.0  # the dispatched run must appear within this
HEALTH_ATTEMPTS = 6
SIGNATURE_HEADER = "X-AI-Coding-Signature"


# ------------------------------------------------------------------ port --
@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    id: uuid.UUID
    name: str
    kind: str  # github_actions | webhook
    production: bool
    requires_approval: bool
    require_gates: bool
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_approval(self) -> bool:
        return self.production or self.requires_approval


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID | None
    ref: str
    requested_by: str
    environment: EnvironmentSpec


class DeployPort(Protocol):
    """What the deploy steps need from the server."""

    def deployment_spec(self, deployment_id: uuid.UUID) -> DeploymentSpec | None: ...
    def claim_deployment(self, deployment_id: uuid.UUID, run_id: uuid.UUID) -> bool: ...
    def update_deployment(self, deployment_id: uuid.UUID, **fields: Any) -> None: ...
    def github_host(self, project_id: uuid.UUID) -> GitHubHost: ...
    def webhook_secret(self, project_id: uuid.UUID, environment: EnvironmentSpec) -> str | None: ...
    def latest_gates(
        self, project_id: uuid.UUID
    ) -> tuple[uuid.UUID, list[dict[str, Any]]] | None: ...
    def deploy_transport(self) -> httpx.AsyncBaseTransport | None: ...


_PORT: DeployPort | None = None


def register_port(port: DeployPort | None) -> None:
    """The server of this process (its deploy service) serves the deploy steps."""
    global _PORT  # noqa: PLW0603 - one server per process (API, worker)
    _PORT = port


def _port() -> DeployPort:
    if _PORT is None:
        raise PermanentError("deployments are not available in this process (no server)")
    return _PORT


# ------------------------------------------------------------- transport --
def public_address(text: str) -> bool:
    """A global unicast address (not loopback, private, link-local, reserved, multicast)."""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_public_url(url: str) -> str:
    """Static check of a webhook or health URL (the transport checks again per request)."""
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or not parsed.host:
        raise ValueError("the URL must be https://host/…")
    host = parsed.host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        if (
            host == "localhost"
            or "." not in host
            or host.endswith((".local", ".internal", ".lan", ".localhost", ".home.arpa"))
        ):
            raise ValueError("the URL must name a public host") from None
        return url
    if not public_address(host):
        raise ValueError("the URL must not point at a loopback, private or link-local address")
    return url


class PublicOnlyTransport(httpx.AsyncBaseTransport):
    """https to public addresses only: resolved, checked and connected to per request."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https":
            raise httpx.ConnectError("only https is allowed", request=request)
        host = request.url.host.rstrip(".").lower()
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, request.url.port or 443, proto=socket.IPPROTO_TCP
            )
        except OSError as exc:
            raise httpx.ConnectError(f"{host} does not resolve ({exc})", request=request) from exc
        _pin(request, host, infos)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class PublicOnlySyncTransport(httpx.BaseTransport):
    """The same rule for synchronous clients (the GitHub API client, ADR-0043)."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https":
            raise httpx.ConnectError("only https is allowed", request=request)
        host = request.url.host.rstrip(".").lower()
        try:
            infos = socket.getaddrinfo(host, request.url.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise httpx.ConnectError(f"{host} does not resolve ({exc})", request=request) from exc
        _pin(request, host, infos)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def _pin(request: httpx.Request, host: str, infos: list[Any]) -> None:
    """Connect to a checked address: every address of the host must be public (a name
    that also resolves to an internal address is refused), and the request goes to the
    address that was checked, with the name kept for Host and TLS (SNI, certificate)."""
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses or not all(public_address(a) for a in addresses):
        raise httpx.ConnectError(
            f"{host} resolves to a non-public address: refused", request=request
        )
    if "host" not in request.headers:
        request.headers["Host"] = request.url.netloc.decode("ascii")
    request.extensions = {**request.extensions, "sni_hostname": host}
    request.url = request.url.copy_with(host=addresses[0])


def _client() -> httpx.AsyncClient:
    transport = _port().deploy_transport() or PublicOnlyTransport()
    return httpx.AsyncClient(
        transport=transport,
        timeout=30.0,
        follow_redirects=False,
        headers={"User-Agent": "ai-coding-platform"},
    )


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------- handlers --
class _Handler:
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    def __init__(self, deployment_id: uuid.UUID) -> None:
        self.deployment_id = deployment_id

    def spec(self) -> DeploymentSpec:
        spec = _port().deployment_spec(self.deployment_id)
        if spec is None:
            raise PermanentError("unknown deployment (the environment may have been removed)")
        return spec

    def host(self, spec: DeploymentSpec) -> GitHubHost:
        """The project's repository; without one (or a token) the deployment fails with
        what to set."""
        try:
            return _port().github_host(spec.project_id)
        except PermanentError:
            raise
        except Exception as exc:  # the port's own error, worded for people
            raise self.fail(str(getattr(exc, "message", exc))) from None

    def fail(self, message: str) -> PermanentError:
        _port().update_deployment(
            self.deployment_id, state="failed", detail=message[:2000], finished=True
        )
        return PermanentError(message)


class ResolveHandler(_Handler):
    name = "builtin.deploy.resolve"
    capabilities = frozenset({"deploy.resolve"})

    async def run(self, ctx: StepContext) -> StepResult:
        spec = self.spec()
        if spec.project_id != ctx.project_id or not _port().claim_deployment(
            self.deployment_id, ctx.run_id
        ):
            raise PermanentError("this deployment belongs to another run")
        env = spec.environment
        commit = await asyncio.to_thread(self._commit, ctx, spec)
        _port().update_deployment(self.deployment_id, commit=commit)
        return StepResult(
            output={
                "environment": env.name,
                "kind": env.kind,
                "ref": spec.ref,
                "commit": commit,
                "production": env.production,
                "approval": env.needs_approval,
            }
        )

    def _commit(self, ctx: StepContext, spec: DeploymentSpec) -> str:
        if spec.environment.kind == "github_actions":
            host = self.host(spec)
            try:
                data = host.client.json("GET", f"/repos/{host.repo}/commits/{spec.ref}")
            except GitHubAPIError as exc:
                raise self.fail(f"ref '{spec.ref}' not found on GitHub ({exc.status})") from None
            finally:
                host.client.close()
            return str(data.get("sha") or "")
        try:
            return LocalGitRepository(ctx.project_root).rev_parse(spec.ref)
        except GitError:
            raise self.fail(f"ref '{spec.ref}' not found in the project repository") from None


class GatesHandler(_Handler):
    name = "builtin.deploy.gates"
    capabilities = frozenset({"deploy.gates"})

    async def run(self, ctx: StepContext) -> StepResult:
        spec = self.spec()
        if not spec.environment.require_gates:
            return StepResult(output={"required": False})
        latest = await asyncio.to_thread(_port().latest_gates, spec.project_id)
        if latest is None:
            raise self.fail(
                "the environment requires passing Quality Gates, "
                "but the project has no completed audit"
            )
        audit_run, gates = latest
        failed = [g for g in gates if g.get("result") == "FAIL" and g.get("blocking")]
        if failed:
            names = ", ".join(str(g.get("gate")) for g in failed)
            raise self.fail(
                f"blocking Quality Gates failed in the latest audit ({names}): fix them first"
            )
        return StepResult(output={"required": True, "audit_run": str(audit_run), "gates": gates})


class ApprovalHandler(_Handler):
    name = "builtin.deploy.approval"
    capabilities = frozenset({"deploy.approval"})

    async def run(self, ctx: StepContext) -> StepResult:
        spec = self.spec()
        env = spec.environment
        if not env.needs_approval:
            return StepResult(output={"approval": "not required"})
        decision = ctx.policy.operation(ApprovalOperation.DEPLOY)
        if decision.verdict is Verdict.REQUIRE_APPROVAL:
            _port().update_deployment(self.deployment_id, state="awaiting_approval")
            where = "production environment" if env.production else "environment"
            ctx.authorize(
                Decision(
                    Verdict.REQUIRE_APPROVAL,
                    "deploy.environment",
                    f"deploy of {spec.ref} to the {where} '{env.name}'",
                    ApprovalOperation.DEPLOY,
                )
            )
        ctx.authorize(decision)  # granted; a denial would stop the run here
        return StepResult(output={"approval": "granted"})


class ExecuteHandler(_Handler):
    name = "builtin.deploy.execute"
    capabilities = frozenset({"deploy.execute"})

    async def run(self, ctx: StepContext) -> StepResult:
        spec = self.spec()
        port = _port()
        port.update_deployment(self.deployment_id, state="running")
        if spec.environment.kind == "github_actions":
            host = await asyncio.to_thread(self.host, spec)
            try:
                output = await self._actions(spec, host)
            finally:
                host.client.close()
        else:
            output = await self._webhook(ctx, spec)
        return StepResult(output=output, evidence_refs=[output["url"]] if output.get("url") else [])

    async def _actions(self, spec: DeploymentSpec, host: GitHubHost) -> dict[str, Any]:
        env = spec.environment
        workflow = str(env.config.get("workflow") or "")
        inputs = {}
        if env.config.get("environment_input"):
            inputs[str(env.config["environment_input"])] = env.name
        if env.config.get("ref_input"):
            inputs[str(env.config["ref_input"])] = spec.ref
        started = time.time()
        try:
            await asyncio.to_thread(host.dispatch_workflow, workflow, ref=spec.ref, inputs=inputs)
        except GitHubAPIError as exc:
            raise self.fail(
                f"GitHub did not start {workflow} ({exc.status}): "
                f"{str(exc).split(': ', 1)[-1][:300]}"
            ) from None
        run = await self._find(host, workflow, spec.ref, started)
        _port().update_deployment(self.deployment_id, external_url=run.get("html_url"))
        limit = min(float(env.config.get("timeout_minutes") or 30), MAX_TIMEOUT_MINUTES) * 60
        while run.get("status") != "completed":
            if time.time() - started > limit:
                await asyncio.to_thread(host.cancel_workflow_run, int(run["id"]))
                raise self.fail(
                    f"the workflow run did not finish within {int(limit // 60)} minutes: cancelled"
                )
            await asyncio.sleep(POLL_SECONDS)
            try:
                run = await asyncio.to_thread(host.workflow_run, int(run["id"]))
            except (TransientError, RateLimitError):
                continue  # GitHub is busy: ask again at the next poll
        if run.get("conclusion") != "success":
            raise self.fail(
                f"the workflow run ended with '{run.get('conclusion')}': {run.get('html_url')}"
            )
        return {
            "kind": "github_actions",
            "workflow": workflow,
            "run_id": run.get("id"),
            "conclusion": "success",
            "url": run.get("html_url"),
        }

    async def _find(
        self, host: GitHubHost, workflow: str, ref: str, started: float
    ) -> dict[str, Any]:
        """The run the dispatch created: the newest workflow_dispatch run created after it."""
        while True:
            try:
                runs = await asyncio.to_thread(host.workflow_runs, workflow, branch=ref, limit=10)
            except (TransientError, RateLimitError):
                runs = []
            for run in runs:
                try:
                    at = datetime.fromisoformat(
                        str(run.get("created_at")).replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    continue
                if at >= started - 15:  # clocks differ a little
                    return run
            if time.time() - started > FIND_SECONDS:
                raise self.fail(f"the dispatched run of {workflow} did not appear on GitHub")
            await asyncio.sleep(POLL_SECONDS)

    async def _webhook(self, ctx: StepContext, spec: DeploymentSpec) -> dict[str, Any]:
        env = spec.environment
        url = str(env.config.get("webhook_url") or "")
        secret = await asyncio.to_thread(_port().webhook_secret, spec.project_id, env)
        if not secret:
            raise self.fail(
                "the environment's webhook signing secret is not set "
                f"({env.config.get('secret_name')})"
            )
        commit = ctx.dependencies.get("deploy.resolve", {}).get("commit")
        body = json.dumps(
            {
                "event": "deploy",
                "project_id": str(spec.project_id),
                "environment": env.name,
                "production": env.production,
                "ref": spec.ref,
                "commit": commit,
                "deployment_id": str(spec.id),
                "run_id": str(ctx.run_id),
                "requested_by": spec.requested_by,
            },
            sort_keys=True,
        ).encode()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(secret, body),
            "X-AI-Coding-Delivery": str(spec.id),
        }
        try:
            async with _client() as client:
                response = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise self.fail(f"the webhook could not be called: {exc}") from None
        if not 200 <= response.status_code < 300:
            raise self.fail(f"the webhook answered {response.status_code}")
        answer: dict[str, Any] = {}
        try:
            data = response.json() if response.content and len(response.content) < 65536 else {}
            answer = data if isinstance(data, dict) else {}
        except ValueError:
            pass
        link = (
            answer.get("url")
            if isinstance(answer.get("url"), str) and str(answer["url"]).startswith("https://")
            else None
        )
        if link:
            _port().update_deployment(self.deployment_id, external_url=link)
        return {"kind": "webhook", "status": response.status_code, "url": link}


class VerifyHandler(_Handler):
    name = "builtin.deploy.verify"
    capabilities = frozenset({"deploy.verify"})

    async def run(self, ctx: StepContext) -> StepResult:
        url = str(self.spec().environment.config.get("health_url") or "")
        if not url:
            return StepResult(output={"health": "not configured"})
        last = "no answer"
        async with _client() as client:
            for attempt in range(HEALTH_ATTEMPTS):
                try:
                    response = await client.get(url)
                    if 200 <= response.status_code < 300:
                        return StepResult(
                            output={"health": response.status_code, "attempts": attempt + 1}
                        )
                    last = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    last = str(exc)
                await asyncio.sleep(POLL_SECONDS)
        raise self.fail(f"the health check did not pass ({last})")


class ReportHandler(_Handler):
    name = "builtin.deploy.report"
    capabilities = frozenset({"report.deploy"})

    async def run(self, ctx: StepContext) -> StepResult:
        spec = self.spec()
        deps = ctx.dependencies
        report = {
            "deployment_id": str(spec.id),
            "environment": spec.environment.name,
            "ref": spec.ref,
            "commit": deps.get("deploy.resolve", {}).get("commit"),
            "gates": deps.get("deploy.gates"),
            "approval": deps.get("deploy.approval"),
            "execution": deps.get("deploy.execute"),
            "health": deps.get("deploy.verify"),
        }
        ref = ctx.save_artifact(
            "deploy-report.json", json.dumps(report, indent=2).encode(), "application/json"
        )
        _port().update_deployment(self.deployment_id, state="succeeded", detail=None, finished=True)
        return StepResult(output={"report": ref.uri, **report}, evidence_refs=[ref.uri])


MAX_TIMEOUT_MINUTES = 120  # an environment's own timeout is enforced by the step


def build_deploy(deployment_id: uuid.UUID) -> tuple[HandlerRegistry, list[StepSpec]]:
    """Handlers and steps of a deploy run."""
    handlers = [
        h(deployment_id)
        for h in (
            ResolveHandler,
            GatesHandler,
            ApprovalHandler,
            ExecuteHandler,
            VerifyHandler,
            ReportHandler,
        )
    ]
    steps = [
        StepSpec(
            key="deploy.resolve",
            capability="deploy.resolve",
            phase=TaskPhase.DISCOVERY,
            timeout_seconds=120,
        ),
        StepSpec(
            key="deploy.gates",
            capability="deploy.gates",
            phase=TaskPhase.DISCOVERY,
            depends_on=("deploy.resolve",),
            timeout_seconds=60,
        ),
        StepSpec(
            key="deploy.approval",
            capability="deploy.approval",
            phase=TaskPhase.EXECUTION,
            depends_on=("deploy.gates",),
            timeout_seconds=60,
        ),
        StepSpec(
            key="deploy.execute",
            capability="deploy.execute",
            phase=TaskPhase.EXECUTION,
            depends_on=("deploy.resolve", "deploy.approval"),
            timeout_seconds=MAX_TIMEOUT_MINUTES * 60 + FIND_SECONDS + 120,
        ),
        StepSpec(
            key="deploy.verify",
            capability="deploy.verify",
            phase=TaskPhase.VERIFICATION,
            depends_on=("deploy.execute",),
            timeout_seconds=HEALTH_ATTEMPTS * (POLL_SECONDS + 35),
        ),
        StepSpec(
            key="report.deploy",
            capability="report.deploy",
            phase=TaskPhase.VERIFICATION,
            depends_on=(
                "deploy.resolve",
                "deploy.gates",
                "deploy.approval",
                "deploy.execute",
                "deploy.verify",
            ),
        ),
    ]
    return HandlerRegistry(handlers), steps


__all__ = [
    "DeployPort",
    "DeploymentSpec",
    "EnvironmentSpec",
    "PublicOnlySyncTransport",
    "PublicOnlyTransport",
    "build_deploy",
    "check_public_url",
    "public_address",
    "register_port",
    "sign",
]
