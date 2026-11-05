"""S3-compatible artifact store (MinIO, Ceph RGW, AWS S3; B-705, ТЗ §4).

Same content-addressed layout as the local store (``<prefix>/sha256/<aa>/<digest>``),
integrity re-checked on every read. Requests are signed with AWS Signature V4 using
``httpx`` (no SDK dependency). Credentials come from the environment only:

| Variable | Meaning |
| --- | --- |
| ``AI_CODING_S3_ENDPOINT`` | e.g. ``https://minio.internal:9000`` |
| ``AI_CODING_S3_BUCKET`` | bucket name |
| ``AI_CODING_S3_ACCESS_KEY_ID`` / ``AI_CODING_S3_SECRET_ACCESS_KEY`` | credentials |
| ``AI_CODING_S3_REGION`` | default ``us-east-1`` |
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from ai_coding.persistence.artifacts import ArtifactIntegrityError, ArtifactRef

ENV = "AI_CODING_S3_"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(RuntimeError):
    """The object store answered with an error."""


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    *,
    payload_sha256: str,
    now: datetime,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
) -> tuple[dict[str, str], str]:
    """AWS Signature V4 headers for a request; ``(headers, canonical query string)``.

    ``path`` must already be URI-encoded. Signed headers: host, x-amz-content-sha256,
    x-amz-date (the minimum S3 requires).
    """
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": payload_sha256, "x-amz-date": amz_date}
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items())
    )
    signed = ";".join(sorted(headers))
    canonical = "\n".join(
        [
            method,
            path,
            canonical_query,
            "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
            signed,
            payload_sha256,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _sign(
        _sign(_sign(_sign(f"AWS4{secret_key}".encode(), day), region), service), "aws4_request"
    )
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers, canonical_query


class S3ArtifactStore:
    """Artifact store in an S3 bucket (path-style requests)."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        prefix: str = "",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not _BUCKET.fullmatch(bucket):
            raise ValueError(f"invalid bucket name {bucket!r}")
        if not endpoint.startswith(("https://", "http://")):
            raise ValueError("the S3 endpoint must be an http(s) URL")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.prefix = "/".join(p for p in prefix.strip("/").split("/") if p not in ("", ".", ".."))
        self._access = access_key
        self._secret = secret_key
        self._client = httpx.Client(transport=transport, timeout=timeout)
        self.root = f"s3://{bucket}/{self.prefix}".rstrip("/")

    @classmethod
    def from_env(cls, *, prefix: str, env: Mapping[str, str] | None = None) -> S3ArtifactStore:
        env = os.environ if env is None else env
        missing = [
            n
            for n in ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")
            if not env.get(ENV + n)
        ]
        if missing:
            raise ValueError(
                "artifacts.store is 'minio' but these variables are not set: "
                + ", ".join(ENV + n for n in missing)
            )
        return cls(
            endpoint=env[ENV + "ENDPOINT"],
            bucket=env[ENV + "BUCKET"],
            access_key=env[ENV + "ACCESS_KEY_ID"],
            secret_key=env[ENV + "SECRET_ACCESS_KEY"],
            region=env.get(ENV + "REGION") or "us-east-1",
            prefix=prefix,
        )

    def close(self) -> None:
        self._client.close()

    # ----------------------------------------------------------------- keys --
    def _key(self, digest: str) -> str:
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"invalid sha256 digest: {digest!r}")
        return "/".join(p for p in (self.prefix, "sha256", digest[:2], digest) if p)

    # -------------------------------------------------------------- signing --
    def _request(
        self,
        method: str,
        key: str = "",
        *,
        body: bytes = b"",
        query: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        url = httpx.URL(self.endpoint)
        path = f"/{self.bucket}" + (f"/{quote(key, safe='/-_.~')}" if key else "")
        host = url.host + (f":{url.port}" if url.port else "")
        payload = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256
        headers, canonical_query = sigv4_headers(
            method,
            host,
            path,
            query or {},
            payload_sha256=payload,
            now=datetime.now(UTC),
            access_key=self._access,
            secret_key=self._secret,
            region=self.region,
        )
        target = f"{self.endpoint}{path}" + (f"?{canonical_query}" if canonical_query else "")
        return self._client.request(method, target, headers=headers, content=body or None)

    @staticmethod
    def _raise(response: httpx.Response, what: str) -> None:
        code = re.search(r"<Code>([^<]+)</Code>", response.text or "")
        raise S3Error(
            f"{what}: HTTP {response.status_code}" + (f" ({code.group(1)})" if code else "")
        )

    # ----------------------------------------------------------------- API --
    def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist (idempotent)."""
        head = self._request("HEAD")
        if head.status_code == 200:
            return
        created = self._request("PUT")
        if created.status_code not in (200, 409):
            self._raise(created, "create bucket")

    def put(self, data: bytes) -> ArtifactRef:
        """Store ``data`` (idempotent: identical content is stored once)."""
        digest = hashlib.sha256(data).hexdigest()
        if not self.exists(digest):
            response = self._request("PUT", self._key(digest), body=data)
            if response.status_code != 200:
                self._raise(response, "put artifact")
        return ArtifactRef(sha256=digest, size_bytes=len(data), uri=f"sha256:{digest}")

    def get(self, digest: str) -> bytes:
        """Read a blob and verify its digest."""
        response = self._request("GET", self._key(digest))
        if response.status_code == 404:
            raise FileNotFoundError(f"artifact {digest} not found")
        if response.status_code != 200:
            self._raise(response, "get artifact")
        data = response.content
        if hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactIntegrityError(f"artifact {digest} is corrupted")
        return data

    def exists(self, digest: str) -> bool:
        return self._request("HEAD", self._key(digest)).status_code == 200

    def delete(self, digest: str) -> bool:
        existed = self.exists(digest)
        if existed:
            response = self._request("DELETE", self._key(digest))
            if response.status_code not in (200, 204):
                self._raise(response, "delete artifact")
        return existed

    def keys(self) -> Iterator[str]:
        """Object keys under the store prefix."""
        token: str | None = None
        while True:
            query: dict[str, Any] = {
                "list-type": "2",
                "prefix": f"{self.prefix}/sha256/" if self.prefix else "sha256/",
            }
            if token:
                query["continuation-token"] = token
            response = self._request("GET", query=query)
            if response.status_code != 200:
                self._raise(response, "list artifacts")
            tree = ElementTree.fromstring(response.content)  # noqa: S314 - our own endpoint
            for node in tree.iter(f"{_NS}Key"):
                if node.text:
                    yield node.text
            next_token = tree.find(f"{_NS}NextContinuationToken")
            if next_token is None or not next_token.text:
                return
            token = next_token.text

    def clear(self) -> int:
        """Delete every artifact of this store (project deletion)."""
        removed = 0
        for key in list(self.keys()):
            response = self._request("DELETE", key)
            if response.status_code in (200, 204):
                removed += 1
        return removed
