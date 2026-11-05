"""B-801 (fuzzing of the configuration): ``.ai-coding.yml`` is untrusted input — it comes
with the repository. Whatever it contains, validation reports problems and never crashes,
and an accepted configuration always keeps the security invariants (ТЗ §12)."""

from __future__ import annotations

import copy
import time
from typing import Any

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ai_coding.config.models import AICodingConfig
from ai_coding.config.validator import validate_document, validate_text
from ai_coding.domain.enums import Mode, NetworkPolicy
from ai_coding.security.endpoints import is_local_endpoint

FUZZ = settings(max_examples=300, deadline=None, database=None,
                suppress_health_check=[HealthCheck.too_slow])  # fmt: skip
WORDS = ["local-only", "open", "allowlist", "ollama", "openai", "audit", "develop", "docker",
         "local", "github-actions", "none", "2.1", "http://ollama:11434",
         "https://api.openai.com/v1", "*", "../.."]  # fmt: skip
KEYS = ["schema_version", "project", "name", "mode", "ai", "security", "network", "execution",
        "backend", "provider", "base_url", "enabled", "model", "allow_host_commands",
        "egress_allowlist", "fallback", "routing", "approvals", "quality_gates", "x"]  # fmt: skip
SCALARS = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True),
                    st.text(max_size=30), st.sampled_from(WORDS))  # fmt: skip
DOCS = st.recursive(
    SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(st.sampled_from(KEYS) | st.text(max_size=8), inner, max_size=5),
    ),
    max_leaves=25,
)
VALID: dict[str, Any] = {"schema_version": "2.1", "project": {"name": "demo"}}
PATHS = [("mode",), ("security", "network"), ("security", "egress_allowlist"),
         ("execution", "backend"), ("execution", "allow_host_commands"), ("ai", "enabled"),
         ("ai", "provider"), ("ai", "model"), ("ai", "base_url"), ("ai", "fallback"),
         ("ai", "api_key_env"), ("project", "name")]  # fmt: skip


def _invariants(config: AICodingConfig) -> None:
    """What an accepted configuration may never allow."""
    if config.security.network is NetworkPolicy.LOCAL_ONLY:
        assert all(p.is_local for p in config.ai.providers_in_use())
        for _where, route in config.ai.routes_in_use():
            assert route.base_url is None or is_local_endpoint(route.base_url)
    if config.mode is Mode.AUDIT:
        assert config.execution.allow_host_commands is False


def _check(document: object) -> None:
    report = validate_document(document)  # must not raise
    if report.ok:
        assert report.config is not None
        _invariants(report.config)


@FUZZ
@given(st.text(max_size=400))
def test_arbitrary_text_never_crashes(text: str) -> None:
    report = validate_text(text)
    assert report.ok or report.issues


@FUZZ
@given(DOCS)
def test_arbitrary_documents_never_crash(document: object) -> None:
    _check(document)


@FUZZ
@given(st.lists(st.tuples(st.sampled_from(PATHS), SCALARS | DOCS), min_size=1, max_size=6))
def test_mutated_valid_configs_keep_the_invariants(
    edits: list[tuple[tuple[str, ...], Any]],
) -> None:
    doc = copy.deepcopy(VALID)
    for path, value in edits:
        node = doc
        for key in path[:-1]:
            node = node.setdefault(key, {}) if isinstance(node.get(key, {}), dict) else node
        node[path[-1]] = value
    _check(doc)
    _check(yaml.safe_load(yaml.safe_dump(doc)))  # the same through YAML


def test_alias_bomb_is_rejected_quickly() -> None:
    """A "billion laughs" document must not exhaust time or memory."""
    lines = ['a: &a ["x","x","x","x","x","x","x","x","x"]']
    lines += [f"{chr(97 + i)}: &{chr(97 + i)} [{','.join(['*' + chr(96 + i)] * 9)}]"
              for i in range(1, 9)]  # fmt: skip
    lines.append("project: *i")
    started = time.monotonic()
    report = validate_text("\n".join(lines))
    assert not report.ok and time.monotonic() - started < 5


def test_ordinary_anchors_still_work() -> None:
    from ai_coding.config.loader import parse_yaml  # noqa: PLC0415

    doc = parse_yaml("defaults: &d {timeout: 60, retries: 2}\nlint: *d\ntests: *d\n")
    assert doc["lint"] == doc["tests"] == {"timeout": 60, "retries": 2}
