"""The egress allowlist proxy (B-205), exercised in-process against local upstreams."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ai_coding.executors.egress import _decisions, proxy_argv
from ai_coding.executors.egress_proxy import (
    Proxy,
    RequestError,
    host_matches,
    public_address,
    rebuild_http_request,
)


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


def test_plain_http_request_is_rebuilt() -> None:
    host, port, head, body = rebuild_http_request(
        "POST", "http://api.example.org:8080/v1?q=1", "HTTP/1.1",
        ["Host: api.example.org:8080", "Content-Length: 5", "Connection: keep-alive",
         "Keep-Alive: timeout=5", "Proxy-Authorization: x", "Accept: */*"],
    )  # fmt: skip
    assert (host, port, body) == ("api.example.org", 8080, 5)
    lines = head.decode().split("\r\n")
    assert lines[0] == "POST /v1?q=1 HTTP/1.1"
    assert "Accept: */*" in lines and lines[-3] == "Connection: close"
    assert not any(n.split(":")[0].lower() in {"keep-alive", "proxy-authorization"}
                   for n in lines[1:-2] if n != "Connection: close")  # fmt: skip
    for bad in (
        ["Host: attacker.example"],  # domain fronting behind an allowed address
        ["Host: api.example.org", "Host: attacker.example"],
        [],  # no Host
        ["Host: api.example.org:9999"],
        ["Host: api.example.org", "Transfer-Encoding: chunked"],
        ["Host: api.example.org", "Content-Length: 1", "Content-Length: 2"],
        ["Host: api.example.org", "Content-Length: -1"],
        ["Host : api.example.org"],
    ):
        with pytest.raises(RequestError):
            rebuild_http_request("GET", "http://api.example.org:8080/", "HTTP/1.1", bad)


async def _recording_http(log: list[bytes]) -> Callable[..., Any]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = b""
        try:
            while chunk := await asyncio.wait_for(reader.read(65536), timeout=1):
                data += chunk
                if b"\r\n\r\n" in data and b"Connection: close" in data:
                    break
        except TimeoutError:
            pass
        log.append(data)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        await asyncio.sleep(0.2)  # anything the proxy still sends would arrive now
        try:
            while more := await asyncio.wait_for(reader.read(65536), timeout=0.3):
                log.append(more)
        except (TimeoutError, OSError):
            pass
        writer.close()

    return handler


async def test_plain_http_cannot_front_or_smuggle_a_second_request() -> None:
    """Domain fronting through an allowed address, and a second request on a kept-alive
    connection, must never reach the upstream (only the target's own request does)."""
    seen: list[bytes] = []
    async with _server(await _recording_http(seen)) as http:
        proxy = Proxy([f"localhost:{http}"], allow_private=True)
        async with _server(proxy.handle) as port:
            fronted = await _ask(port, f"GET http://localhost:{http}/ HTTP/1.1\r\n"
                                       f"Host: attacker.example\r\n\r\n".encode())  # fmt: skip
            assert b"403" in fronted and b"does not match" in fronted and not seen

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                f"POST http://localhost:{http}/up HTTP/1.1\r\nHost: localhost:{http}\r\n"
                f"Content-Length: 4\r\nConnection: keep-alive\r\n\r\nbody"
                f"GET /exfil HTTP/1.1\r\nHost: attacker.example\r\n\r\n".encode()
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()
    assert response.startswith(b"HTTP/1.1 200") and response.endswith(b"ok")
    upstream = b"".join(seen)
    assert upstream.startswith(b"POST /up HTTP/1.1\r\n") and upstream.endswith(b"\r\n\r\nbody")
    assert b"Connection: close" in upstream and b"keep-alive" not in upstream
    assert b"exfil" not in upstream and b"attacker" not in upstream
