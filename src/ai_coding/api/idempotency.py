"""``Idempotency-Key`` (ТЗ §13): a request repeated with the same key returns the
stored response instead of running again."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ai_coding.api.core import (
    InvalidError,
    Principal,
    ServerCore,
)
from ai_coding.persistence.models import (
    IdempotencyRecord,
)

_KEY = re.compile(r"[\x21-\x7e]{1,200}")


class IdempotencyService(ServerCore):
    """Replay of responses by (user, Idempotency-Key)."""

    def idempotent(
        self,
        principal: Principal,
        key: str,
        *,
        method: str,
        path: str,
        body: bytes,
        produce: Callable[[], tuple[int, dict[str, Any]]],
    ) -> tuple[int, dict[str, Any], bool]:
        """Run ``produce`` once per ``(user, Idempotency-Key)``; replay its response.

        Reusing a key for a different method, path or body is refused (422). Only
        successful responses are stored, so a failed request can be retried.
        Returns ``(status, payload, replayed)``.
        """
        if not _KEY.fullmatch(key):
            raise InvalidError("Idempotency-Key must be 1-200 visible ASCII characters")
        digest = hashlib.sha256(body).hexdigest()
        stored = self._replay(principal, key, method, path, digest)
        if stored is not None:
            return stored[0], stored[1], True
        status, payload = produce()
        try:
            with self.session() as s:
                s.add(
                    IdempotencyRecord(
                        user_id=principal.user_id,
                        key=key,
                        method=method,
                        path=path[:500],
                        body_sha256=digest,
                        status_code=status,
                        response=payload,
                    )
                )
        except IntegrityError:  # a concurrent request with the same key won the race
            stored = self._replay(principal, key, method, path, digest)
            if stored is not None:
                return stored[0], stored[1], True
        return status, payload, False

    def _replay(
        self, principal: Principal, key: str, method: str, path: str, digest: str
    ) -> tuple[int, dict[str, Any]] | None:
        with self.session() as s:
            rec = s.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.user_id == principal.user_id, IdempotencyRecord.key == key
                )
            )
            if rec is None:
                return None
            if (rec.method, rec.path, rec.body_sha256) != (method, path[:500], digest):
                raise InvalidError("Idempotency-Key was already used for a different request")
            return rec.status_code, dict(rec.response)
