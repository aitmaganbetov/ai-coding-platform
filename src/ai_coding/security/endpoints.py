"""Is an endpoint inside the local environment? (ТЗ §12: ``local-only`` forbids egress)

A "local" model provider type (Ollama, vLLM) is not enough: its ``base_url`` decides
where prompts — which contain source code — are sent. Two checks:

* :func:`is_local_endpoint` — static, for configuration validation: loopback and
  private IP literals, ``localhost``, single-label names (Docker/Kubernetes service
  names) and the private-use suffixes ``.local``, ``.internal``, ``.lan``,
  ``.home.arpa``, ``.localhost``;
* :func:`resolves_locally` — when the model is set up, every address the name resolves
  to must be loopback, private or link-local (an early, readable refusal);
* :class:`LocalOnlyTransport` — on *every* request: the HTTP client resolves the name
  itself, checks the addresses and connects to the checked address. Checking once and
  letting the client resolve again would let a short-TTL DNS answer point the name
  outside in between (DNS rebinding).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

import httpx

LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa", ".localhost")

Resolver = Callable[[str], Iterable[str]]


def _host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.rstrip(".").lower()


def _local_ip(text: str) -> bool | None:
    """``True``/``False`` for an IP literal, ``None`` for a name."""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def is_local_endpoint(url: str) -> bool:
    """Whether ``url`` names a host of the local environment (no DNS lookup)."""
    host = _host(url)
    if not host:
        return False
    literal = _local_ip(host)
    if literal is not None:
        return literal
    return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES)


def _system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def resolves_locally(url: str, resolver: Resolver | None = None) -> tuple[bool, str]:
    """``(ok, reason)``: every address of the host is loopback/private/link-local."""
    host = _host(url)
    if not host:
        return False, f"no host in {url!r}"
    literal = _local_ip(host)
    if literal is not None:
        return literal, "" if literal else f"{host} is a public address"
    try:
        addresses = sorted(set((resolver or _system_resolver)(host)))
    except OSError as exc:
        return False, f"{host} does not resolve ({exc})"
    public = [a for a in addresses if not _local_ip(a)]
    if not addresses:
        return False, f"{host} does not resolve"
    if public:
        return False, f"{host} resolves to public address(es) {', '.join(public)}"
    return True, ""


class LocalOnlyTransport(httpx.AsyncBaseTransport):
    """Sends a request only to a local address, resolved and checked for this request.

    The request goes to the checked IP address; the ``Host`` header keeps the name, and
    over HTTPS the TLS server name (and certificate check) stays the name as well.
    """

    def __init__(
        self, inner: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._resolver = resolver

    async def _addresses(self, host: str, port: int) -> list[str]:
        resolver = self._resolver
        if resolver is not None:
            return sorted(set(await asyncio.to_thread(lambda: list(resolver(host)))))
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        return sorted({str(info[4][0]) for info in infos})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.rstrip(".").lower()
        literal = _local_ip(host)
        if literal is False:
            raise httpx.ConnectError(f"local-only policy: {host} is a public address",
                                     request=request)  # fmt: skip
        if literal is None:
            try:
                addresses = await self._addresses(host, request.url.port or 0)
            except OSError as exc:
                raise httpx.ConnectError(f"local-only policy: {host} does not resolve ({exc})",
                                         request=request) from exc  # fmt: skip
            public = [a for a in addresses if not _local_ip(a)]
            if not addresses or public:
                raise httpx.ConnectError(
                    f"local-only policy: {host} resolves to public address(es) "
                    f"{', '.join(public) or '(none)'}",
                    request=request,
                )
            if "host" not in request.headers:
                request.headers["Host"] = request.url.netloc.decode("ascii")
            if request.url.scheme == "https":
                request.extensions = {**request.extensions, "sni_hostname": host}
            request.url = request.url.copy_with(host=addresses[0])
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()
