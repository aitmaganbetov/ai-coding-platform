"""Egress allowlist proxy for networked sandbox steps (ТЗ §12, B-205).

This file is executed *inside* a container (``python -I -c <source>``) and uses only
the standard library. The sandbox container sits on an internal Docker network with no
route outside; this proxy is the only way out and lets through:

* ``CONNECT host:443`` (HTTPS) and ``GET http://host/…`` (plain HTTP, port 80) only to
  hosts matching the allowlist (``host`` exactly or ``*.example.org`` for subdomains —
  the platform policy's rules);
* only when every address the name resolves to is a public address: an allowed name
  that points at a private, loopback or link-local address (cloud metadata service,
  internal services, DNS rebinding) is refused.

Plain HTTP is forwarded one request per connection, rebuilt by the proxy: the ``Host``
header must name the allowed target (no virtual host behind an allowed address — domain
fronting), the body is read by ``Content-Length`` only, the upstream is told
``Connection: close``, and nothing more the client sends reaches it (no second request
on a kept-alive connection). Inside a CONNECT tunnel the proxy sees only TLS: it cannot
check the TLS server name or the encrypted ``Host``, so an allowlisted name on a shared
CDN can still front another site there (ADR-0033, residual risk).

Every decision is printed as one JSON line (the platform attaches denials to the step's
result). Anything else — other methods, ports, malformed requests — is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import socket
import sys

HEAD_LIMIT = 16 * 1024
IDLE_SECONDS = 300.0
CONNECT_PORTS = frozenset({443})
HTTP_PORTS = frozenset({80})
MAX_HTTP_BODY = 64 * 1024 * 1024
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-connection", "proxy-authorization",
                         "proxy-authenticate", "te", "trailer", "upgrade"})  # fmt: skip


class RequestError(ValueError):
    """A plain-HTTP request the proxy will not forward."""


def rebuild_http_request(method: str, target: str, version: str,
                         header_lines: list[str]) -> tuple[str, int, bytes, int]:  # fmt: skip
    """``(host, port, head, body_length)`` of one plain-HTTP proxy request, or raise.

    The head is rebuilt in origin form with the hop-by-hop headers removed and
    ``Connection: close`` added; the ``Host`` header must name the target.
    """
    authority, _, path = target[len("http://") :].partition("/")
    host, _, port_text = authority.partition(":")
    port = int(port_text or 80)
    if not host:
        raise RequestError("no host in the request target")
    headers: list[tuple[str, str]] = []
    for line in header_lines:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise RequestError("malformed header line")
        headers.append((name, value.strip()))
    lower = [(n.lower(), v) for n, v in headers]
    hosts = [v for n, v in lower if n == "host"]
    if len(hosts) != 1:
        raise RequestError("exactly one Host header is required")
    h_host, _, h_port = hosts[0].partition(":")
    # The connection's port is the target's; a Host without a port is common practice.
    if h_host.lower().rstrip(".") != host.lower().rstrip(".") or (h_port and int(h_port) != port):
        raise RequestError(f"Host header '{hosts[0]}' does not match the target '{authority}'")
    if any(n == "transfer-encoding" for n, _ in lower):
        raise RequestError("Transfer-Encoding request bodies are not forwarded")
    lengths = {v for n, v in lower if n == "content-length"}
    if len(lengths) > 1 or not all(v.isdigit() for v in lengths):
        raise RequestError("invalid Content-Length")
    body = int(next(iter(lengths))) if lengths else 0
    if body > MAX_HTTP_BODY:
        raise RequestError("request body too large")
    kept = [f"{n}: {v}" for n, v in headers if n.lower() not in _HOP_BY_HOP]
    head = "\r\n".join([f"{method} /{path} {version}", *kept, "Connection: close", "", ""])
    return host, port, head.encode("latin-1"), body


def host_matches(host: str, pattern: str) -> bool:
    """The platform policy's matching (orchestrator.policy._host_matches)."""
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        base = pattern[2:]
        return host == base or host.endswith("." + base)
    return host == pattern


def public_address(text: str) -> bool:
    ip = ipaddress.ip_address(text.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def log(**fields: object) -> None:
    print(json.dumps({"event": "egress", **fields}, sort_keys=True), flush=True)


class Proxy:
    def __init__(self, allow: list[str], *, allow_private: bool = False) -> None:
        self.allow = [a.lower() for a in allow]
        self.allow_private = allow_private  # operator-named internal hosts (k6 targets)

    def allowed(self, host: str, port: int, default_ports: frozenset[int]) -> bool:
        """``host:port`` entries allow exactly that port; plain entries the default ones."""
        for entry in self.allow:
            name, _, wanted = entry.rpartition(":") if entry.count(":") == 1 else (entry, "", "")
            if host_matches(host, name or entry) and (
                port == int(wanted) if wanted else port in default_ports
            ):
                return True
        return False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.close()
            return
        try:
            line, _, rest = head.decode("latin-1").partition("\r\n")
            method, target, version = line.split(" ", 2)
            if method == "CONNECT":
                host, _, port_text = target.rpartition(":")
                await self.tunnel(writer, reader, host.strip("[]"), int(port_text), None, version)
            elif method in {"GET", "HEAD", "POST", "PUT"} and target.startswith("http://"):
                lines = [h for h in rest.split("\r\n") if h]
                try:
                    host, port, origin, body = rebuild_http_request(method, target, version,
                                                                    lines)  # fmt: skip
                except RequestError as exc:
                    await self.refuse(writer, method, target, str(exc))
                    return
                await self.tunnel(writer, reader, host, port, origin, version, body)
            else:
                await self.refuse(writer, method, target, "method or target not allowed")
        except (ValueError, OSError) as exc:
            await self.refuse(writer, "?", "?", f"bad request: {exc}")

    async def refuse(self, writer: asyncio.StreamWriter, method: str, target: str,
                     reason: str) -> None:  # fmt: skip
        log(allowed=False, method=method, target=target, reason=reason)
        body = f"egress denied by the AI Coding Platform sandbox policy: {reason}\n"
        writer.write(f"HTTP/1.1 403 Forbidden\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n{body}".encode())  # fmt: skip
        with contextlib.suppress(OSError):
            await writer.drain()
        writer.close()

    async def tunnel(  # noqa: PLR0917 - one connection's whole state
        self, writer: asyncio.StreamWriter, reader: asyncio.StreamReader,
        host: str, port: int, first: bytes | None, version: str, body_length: int = 0,
    ) -> None:  # fmt: skip
        target = f"{host}:{port}"
        host = host.lower().rstrip(".")  # match and resolve the same, normalised name
        method = "CONNECT" if first is None else "HTTP"
        ports = CONNECT_PORTS if first is None else HTTP_PORTS
        if not self.allowed(host, port, ports):
            await self.refuse(writer, method, target, f"'{host}:{port}' is not in the allowlist")
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            await self.refuse(writer, method, target, f"cannot resolve: {exc}")
            return
        addresses = sorted({str(i[4][0]) for i in infos})
        private = [] if self.allow_private else [a for a in addresses if not public_address(a)]
        if private or not addresses:
            reason = f"resolves to non-public address(es) {', '.join(private)}"
            await self.refuse(writer, method, target, reason)
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port), timeout=15
            )
        except (OSError, TimeoutError) as exc:
            await self.refuse(writer, method, target, f"upstream unreachable: {exc}")
            return
        log(allowed=True, method=method, target=target, address=addresses[0])
        if first is None:
            writer.write(f"{version} 200 Connection established\r\n\r\n".encode())
            await asyncio.gather(self.pipe(reader, up_writer), self.pipe(up_reader, writer))
            return
        # Plain HTTP: this one request and its declared body, then only the response.
        up_writer.write(first)
        try:
            remaining = body_length
            while remaining:
                chunk = await asyncio.wait_for(reader.read(min(65536, remaining)),
                                               timeout=IDLE_SECONDS)  # fmt: skip
                if not chunk:
                    break
                up_writer.write(chunk)
                await up_writer.drain()
                remaining -= len(chunk)
        except (OSError, TimeoutError):
            pass
        await self.pipe(up_reader, writer)
        with contextlib.suppress(OSError):
            up_writer.close()

    @staticmethod
    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while data := await asyncio.wait_for(src.read(65536), timeout=IDLE_SECONDS):
                dst.write(data)
                await dst.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.close()


async def serve(port: int, allow: list[str], *, allow_private: bool = False) -> None:
    proxy = Proxy(allow, allow_private=allow_private)
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", port, limit=HEAD_LIMIT)  # noqa: S104
    log(ready=True, port=port, allow=allow, allow_private=allow_private)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="egress-proxy")
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--allow-private", action="store_true")
    args = parser.parse_args(argv)
    asyncio.run(serve(args.port, args.allow, allow_private=args.allow_private))


if __name__ == "__main__":
    main(sys.argv[1:])
