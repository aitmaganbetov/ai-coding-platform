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
    def __init__(self, allow: list[str]) -> None:
        self.allow = [a.lower() for a in allow]

    def allowed(self, host: str) -> bool:
        return any(host_matches(host, p) for p in self.allow)

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
                host, _, port = target.rpartition(":")
                await self.tunnel(writer, reader, host.strip("[]"), int(port), None, version)
            elif method in {"GET", "HEAD", "POST", "PUT"} and target.startswith("http://"):
                authority, _, path = target[len("http://") :].partition("/")
                host, _, port = authority.partition(":")
                origin = f"{method} /{path} {version}\r\n{rest}".encode("latin-1")
                await self.tunnel(writer, reader, host, int(port or 80), origin, version)
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
        host: str, port: int, first: bytes | None, version: str,
    ) -> None:  # fmt: skip
        target = f"{host}:{port}"
        host = host.lower().rstrip(".")  # match and resolve the same, normalised name
        method = "CONNECT" if first is None else "HTTP"
        ports = CONNECT_PORTS if first is None else HTTP_PORTS
        if port not in ports:
            await self.refuse(writer, method, target, f"port {port} not allowed")
            return
        if not self.allowed(host):
            await self.refuse(writer, method, target, f"'{host}' is not in the allowlist")
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            await self.refuse(writer, method, target, f"cannot resolve: {exc}")
            return
        addresses = sorted({str(i[4][0]) for i in infos})
        private = [a for a in addresses if not public_address(a)]
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
        else:
            up_writer.write(first)
        await asyncio.gather(self.pipe(reader, up_writer), self.pipe(up_reader, writer))

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


async def serve(port: int, allow: list[str]) -> None:
    proxy = Proxy(allow)
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", port, limit=HEAD_LIMIT)  # noqa: S104
    log(ready=True, port=port, allow=allow)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="egress-proxy")
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)
    asyncio.run(serve(args.port, args.allow))


if __name__ == "__main__":
    main(sys.argv[1:])
