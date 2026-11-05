"""B-801: secrets are redacted wherever they appear in text (logs, prompts, reports)."""

from __future__ import annotations

import secrets
import string

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_coding.api.security import new_token
from ai_coding.security import redact

ALNUM = string.ascii_letters + string.digits
UPPER = string.ascii_uppercase + string.digits


def _rand(alphabet: str, n: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


GENERATORS = {
    "aws": lambda: "AKIA" + _rand(UPPER, 16),
    "github": lambda: "ghp_" + _rand(ALNUM, 36),
    "openai": lambda: "sk-proj-" + _rand(ALNUM, 40),
    "anthropic": lambda: "sk-ant-api03-" + _rand(ALNUM, 40),
    "slack": lambda: "xoxb-" + _rand(ALNUM, 24),
    "google": lambda: "AIza" + _rand(ALNUM, 35),
    "ai-coding": new_token,
}
SEPARATORS = st.text(alphabet=" \t\n,;:()[]{}<>\"'|=`", min_size=1, max_size=3)
NOISE = st.text(alphabet=ALNUM + " .-_/", max_size=40)


@settings(max_examples=400, deadline=None, database=None)
@given(kind=st.sampled_from(sorted(GENERATORS)), before=NOISE, after=NOISE,
       left=SEPARATORS, right=SEPARATORS)  # fmt: skip
def test_secret_is_redacted_in_any_context(
    kind: str, before: str, after: str, left: str, right: str
) -> None:
    secret = GENERATORS[kind]()
    text = f"{before}{left}{secret}{right}{after}"
    assert secret not in redact(text), (kind, text)


def test_platform_tokens_in_logs_are_redacted() -> None:
    token = new_token()
    line = f"request failed for {token} (401)"
    assert token not in redact(line) and token.startswith("aic_")
