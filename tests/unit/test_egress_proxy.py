"""The egress allowlist proxy (B-205), exercised in-process against local upstreams."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ai_coding.executors.egress import _decisions, proxy_argv
from ai_coding.executors.egress_proxy import Proxy, host_matches, public_address


def test_matching_and_addresses() -> None:
    assert host_matches("registry.npmjs.org", "registry.npmjs.org")
    assert host_matches("a.b.example.org", "*.example.org") and host_matches(
        "example.org", "*.example.org"
    )
    assert not host_matches("evil-example.org", "*.example.org")
    assert host_matches("pypi.org.", "pypi.org")  # trailing dot
    assert public_address("93.184.216.34") and not public_address("10.0.0.1")
    assert not public_address("169.254.169.254") and not public_address("::ffff:127.0.0.1")
    proxy = Proxy(["pypi.org", "staging.internal:8080", "*.example.org"])
    assert proxy.allowed("pypi.org", 443, frozenset({443}))
    assert not proxy.allowed("pypi.org", 8443, frozenset({443}))
    assert proxy.allowed("staging.internal", 8080, frozenset({80}))
    assert not proxy.allowed("staging.internal", 80, frozenset({80}))  # only the named port
    assert proxy.allowed("api.example.org", 443, frozenset({443}))


@asynccontextmanager
async def _server(handler: Callable[..., Any]) -> AsyncIterator[int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    data = await reader.read(100)
    writer.write(b"echo:" + data)
    await writer.drain()
    writer.close()


async def _http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    head = await reader.readuntil(b"\r\n\r\n")
    line = head.split(b"\r\n", 1)[0]
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(line), line))
    await writer.drain()
    writer.close()


async def _ask(proxy_port: int, request: bytes, payload: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(request)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    if payload and head.startswith(b"HTTP/1.1 200"):
        writer.write(payload)
        await writer.drain()
    rest = await reader.read(1000)
    writer.close()
    return head + rest


async def test_tunnels_only_what_is_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    async with _server(_echo) as echo, _server(_http) as http:
        for allow_private, expect_open in ((False, False), (True, True)):
            proxy = Proxy([f"127.0.0.1:{echo}", f"localhost:{http}"], allow_private=allow_private)
            async with _server(proxy.handle) as port:
                tunnel = await _ask(
                    port, f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\n\r\n".encode(), b"hi"
                )
                plain = await _ask(port, f"GET http://localhost:{http}/x?y HTTP/1.1\r\n"
                                         f"Host: localhost\r\n\r\n".encode())  # fmt: skip
                if expect_open:  # operator-named internal hosts (k6 targets)
                    assert tunnel.endswith(b"echo:hi") and b"200 Connection established" in tunnel
                    assert plain.startswith(b"HTTP/1.1 200") and plain.endswith(
                        b"GET /x?y HTTP/1.1"
                    )
                else:  # registry hosts: a private address is refused (SSRF)
                    assert b"403" in tunnel and b"non-public" in tunnel
                    assert b"403" in plain
                other = await _ask(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
                assert b"403" in other and b"not in the allowlist" in other
                port_mismatch = await _ask(
                    port, f"CONNECT 127.0.0.1:{echo + 1} HTTP/1.1\r\n\r\n".encode()
                )
                assert b"403" in port_mismatch
                for bad in (b"DELETE http://x/ HTTP/1.1\r\n\r\n", b"nonsense\r\n\r\n"):
                    assert b"403" in await _ask(port, bad)
    decisions = _decisions(capsys.readouterr().out)
    assert any(d["allowed"] is True for d in decisions)
    assert any(d["allowed"] is False and "allowlist" in str(d["reason"]) for d in decisions)
    assert all(json.dumps(d) for d in decisions)


def test_proxy_container_flags() -> None:
    assert "--allow-private" in proxy_argv("p", ("x.internal:8080",), allow_private=True)
    assert "--allow-private" not in proxy_argv("p", ("pypi.org",))
