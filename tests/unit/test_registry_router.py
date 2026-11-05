"""Handler registry and Agent Router (FR-08)."""

from __future__ import annotations

import pytest

from ai_coding.orchestrator.errors import NoRouteError
from ai_coding.orchestrator.policy import PolicyEngine
from ai_coding.orchestrator.registry import HandlerRegistry
from ai_coding.orchestrator.router import AgentRouter
from ai_coding.persistence import Database
from ai_coding.persistence.models import Agent
from tests.helpers import handler, make_config


def test_register_validation() -> None:
    reg = HandlerRegistry([handler("a", "cap")])
    with pytest.raises(ValueError, match="already registered"):
        reg.register(handler("a", "cap"))
    with pytest.raises(TypeError):
        reg.register(object())  # type: ignore[arg-type]
    bad = handler("b", "x")
    bad.capabilities = frozenset()
    with pytest.raises(ValueError, match="capability"):
        reg.register(bad)
    with pytest.raises(KeyError, match="no handler named"):
        reg.get("zzz")
    assert reg.get("a").name == "a"
    assert reg.load_entry_points() == []


def test_router_prefers_cheapest_then_name() -> None:
    reg = HandlerRegistry(
        [
            handler("z", "cap", cost_usd=0.1),
            handler("b", "cap", cost_usd=0.5),
            handler("a", "cap", cost_usd=0.1),
        ]
    )
    router = AgentRouter(reg, PolicyEngine(make_config()))
    assert router.route("cap").handler.name == "a"
    assert router.route("cap", preferred="b").handler.name == "b"


def test_router_rejections_explained() -> None:
    reg = HandlerRegistry(
        [
            handler("reviewer", "review"),  # an agent name that we disable
            handler("pricey", "review", cost_usd=5.0),
            handler("offline", "review", available=False),
        ]
    )
    router = AgentRouter(reg, PolicyEngine(make_config(agents__enabled=["qa"])))
    with pytest.raises(NoRouteError) as info:
        router.route("review", max_cost_usd=1.0)
    text = str(info.value)
    assert "reviewer: agent 'reviewer' is not enabled" in text
    assert "pricey: cost" in text
    assert "offline: not available" in text
    with pytest.raises(NoRouteError, match="no handler registered"):
        router.route("unknown")


def test_sync_to_db(db: Database) -> None:
    reg = HandlerRegistry([handler("a", "x", "y", cost_usd=0.2)])
    with db.session() as s:
        reg.sync_to_db(s)
    reg2 = HandlerRegistry([handler("a", "z")])
    with db.session() as s:
        reg2.sync_to_db(s)
    with db.session() as s:
        agent = s.query(Agent).one()
        assert agent.capabilities == ["z"] and agent.cost_per_call_usd == 0.0
