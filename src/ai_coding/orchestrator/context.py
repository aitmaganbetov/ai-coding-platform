"""Context Manager: repository index and minimal relevant context (ТЗ §5, FR-18).

* Only *reads* files; never executes anything from the repository.
* Secret-bearing paths (``.env``, keys, credentials…) are excluded from the index
  entirely; remaining content is passed through the redactor.
* Every excerpt is wrapped as **untrusted data** so that downstream prompts can
  keep it separate from instructions (ТЗ §12, AC-07).
"""

from __future__ import annotations

import fnmatch
import math
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ai_coding.security import redact

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git/*", "*/.git/*", "node_modules/*", "*/node_modules/*", "vendor/*", "*/vendor/*",
    ".venv/*", "venv/*", "*/__pycache__/*", "dist/*", "build/*", "target/*", ".ai-coding/*",
    # secret-bearing files: never indexed (FR-18)
    ".env", ".env.*", "*/.env", "*/.env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "id_rsa*", "*/id_rsa*", "id_ed25519*", "*/id_ed25519*", "*.kdbx", "secrets/*", "*/secrets/*",
    "*credentials*", "*.tfstate", "*.tfstate.*", ".npmrc", ".pypirc", ".netrc", "*/.npmrc",
)  # fmt: skip
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CHARS_PER_TOKEN = 4
UNTRUSTED_OPEN = "<untrusted-repository-content path={path!r}>"
UNTRUSTED_CLOSE = "</untrusted-repository-content>"


@dataclass(frozen=True, slots=True)
class IndexedFile:
    """Metadata of one indexed text file."""

    path: str
    size_bytes: int
    terms: Counter[str] = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One excerpt selected for a prompt."""

    path: str
    score: float
    text: str
    truncated: bool
    tokens: int


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Selected context within a token budget."""

    items: tuple[ContextItem, ...]
    total_tokens: int
    excluded_secret_paths: int

    def render(self) -> str:
        """Items wrapped in untrusted-content markers."""
        return "\n".join(
            f"{UNTRUSTED_OPEN.format(path=i.path)}\n{i.text}\n{UNTRUSTED_CLOSE}" for i in self.items
        )


def estimate_tokens(text: str) -> int:
    """Rough token estimate (≈4 characters per token)."""
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN)) if text else 0


class ContextManager:
    """Indexes a repository and selects the most relevant files for a query."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 200_000,
        max_files: int = 20_000,
        excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    ) -> None:
        self.root = root.resolve()
        self._max_file_bytes = max_file_bytes
        self._max_files = max_files
        self._excludes = excludes
        self._index: dict[str, IndexedFile] | None = None
        self.excluded_secret_paths = 0

    def _is_excluded(self, rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, p) for p in self._excludes)

    def _candidate_paths(self) -> list[str]:
        git = shutil.which("git")
        if git and (self.root / ".git").exists():
            proc = subprocess.run(  # noqa: S603 - argv list, fixed arguments
                [git, "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached", "--others",
                 "--exclude-standard"],
                cwd=self.root, capture_output=True, text=True, timeout=60, check=False,
            )  # fmt: skip
            if proc.returncode == 0:
                return sorted(p for p in proc.stdout.split("\0") if p)
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def build_index(self) -> dict[str, IndexedFile]:
        """(Re)build the index of readable, non-secret text files."""
        index: dict[str, IndexedFile] = {}
        self.excluded_secret_paths = 0
        for rel in self._candidate_paths():
            if len(index) >= self._max_files:
                break
            if self._is_excluded(rel):
                self.excluded_secret_paths += 1
                continue
            path = self.root / rel
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                if size > self._max_file_bytes:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8192]:
                continue  # binary
            text = raw.decode("utf-8", errors="replace")
            terms = Counter(t.lower() for t in _TOKEN_RE.findall(text))
            terms.update(t.lower() for t in _TOKEN_RE.findall(rel))
            index[rel] = IndexedFile(rel, size, terms)
        self._index = index
        return index

    @property
    def index(self) -> dict[str, IndexedFile]:
        """Lazily built index."""
        if self._index is None:
            self.build_index()
        assert self._index is not None  # noqa: S101 - set by build_index
        return self._index

    def select(self, query: str, *, max_tokens: int = 8000, max_files: int = 20) -> ContextBundle:
        """Pick the most relevant files for ``query`` within ``max_tokens`` (TF-IDF scoring)."""
        index = self.index
        q_terms = {t.lower() for t in _TOKEN_RE.findall(query)}
        n_docs = max(1, len(index))
        df = Counter(t for f in index.values() for t in q_terms if t in f.terms)
        scored: list[tuple[float, str]] = []
        for rel, entry in index.items():
            score = 0.0
            for term in q_terms:
                tf = entry.terms.get(term, 0)
                if tf:
                    score += (1 + math.log(tf)) * math.log(1 + n_docs / (1 + df[term]))
            if any(term in rel.lower() for term in q_terms):
                score *= 2 if score else 1
                score += 1.0
            if score > 0:
                scored.append((score, rel))
        scored.sort(key=lambda x: (-x[0], x[1]))
        items: list[ContextItem] = []
        used = 0
        for score, rel in scored[:max_files]:
            remaining = max_tokens - used
            if remaining <= 0:
                break
            text = redact((self.root / rel).read_text(encoding="utf-8", errors="replace"))
            truncated = False
            if estimate_tokens(text) > remaining:
                text = text[: remaining * _CHARS_PER_TOKEN]
                truncated = True
            tokens = estimate_tokens(text)
            used += tokens
            items.append(ContextItem(rel, round(score, 4), text, truncated, tokens))
        return ContextBundle(tuple(items), used, self.excluded_secret_paths)
