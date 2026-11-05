"""Is an endpoint inside the local environment? (ТЗ §12: ``local-only`` forbids egress)

A "local" model provider type (Ollama, vLLM) is not enough: its ``base_url`` decides
where prompts — which contain source code — are sent. Two checks:

* :func:`is_local_endpoint` — static, for configuration validation: loopback and
  private IP literals, ``localhost``, single-label names (Docker/Kubernetes service
  names) and the private-use suffixes ``.local``, ``.internal``, ``.lan``,
  ``.home.arpa``, ``.localhost``;
* :func:`resolves_locally` — at run time, every address the name resolves to must be
  loopback, private or link-local, so DNS cannot redirect a "local" name outside.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

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
