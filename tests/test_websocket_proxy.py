"""auth_proxy.py must tunnel WebSocket upgrades (Jellyfin's /socket) instead of answering them as plain HTTP.

Before, _proxy() dropped Upgrade/Connection as hop-by-hop and sent the handshake through http.client, so Jellyfin saw
a plain GET /socket, answered 404, and the web client reconnected every few seconds: no live updates (now playing,
remote control, scan progress).
"""

import importlib.util
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.http11 import Request, Response
from websockets.sync.client import connect
from websockets.sync.server import ServerConnection, serve

_AUTH_PROXY = Path(__file__).resolve().parent.parent / "auth_proxy.py"


@pytest.fixture(scope="module")
def auth_proxy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("auth_proxy", _AUTH_PROXY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Upstream:
    """A fake Jellyfin: echoes messages on /socket, refuses other paths, and records each handshake's headers."""

    def __init__(self) -> None:
        self.handshakes: list[Headers] = []
        self.closed = threading.Event()
        self._server = serve(self._handler, "127.0.0.1", 0, process_request=self._process_request, max_size=None)
        self.port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        self.handshakes.append(request.headers)
        if not request.path.startswith("/socket"):
            return connection.respond(401, "nope\n")
        return None

    def _handler(self, ws: ServerConnection) -> None:
        try:
            for message in ws:
                if message == "close-from-server":
                    ws.close(code=4000, reason="bye")
                    return
                ws.send(message)
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()

    def shutdown(self) -> None:
        self._server.shutdown()


@pytest.fixture
def upstream() -> Iterator[_Upstream]:
    server = _Upstream()
    yield server
    server.shutdown()


def _start_proxy(auth_proxy: ModuleType, upstream_port: int) -> tuple[Any, int]:
    handler = type(
        "Handler",
        (auth_proxy.AuthProxyHandler,),
        {"upstream_host": "127.0.0.1", "upstream_port": upstream_port, "token_file": "/nonexistent"},
    )
    server = auth_proxy.IPv4ThreadingServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def proxy_port(auth_proxy: ModuleType, upstream: _Upstream) -> Iterator[int]:
    server, port = _start_proxy(auth_proxy, upstream.port)
    yield port
    server.shutdown()
    server.server_close()


def _raw_upgrade(path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
    ).encode()


# What the router adds when proxying an owner's WebSocket.
_ROUTER_HEADERS = {"X-Forwarded-Host": "jellyfin.zone.example", "X-OpenHost-Is-Owner": "true"}


def test_messages_flow_both_ways(proxy_port: int, upstream: _Upstream) -> None:
    with connect(
        f"ws://127.0.0.1:{proxy_port}/socket?ApiKey=abc", additional_headers=_ROUTER_HEADERS, max_size=None
    ) as ws:
        ws.send("hello")
        assert ws.recv(timeout=5) == "hello"
        ws.send(b"\x00\x01binary")
        assert ws.recv(timeout=5) == b"\x00\x01binary"
        big = "x" * (2 * 1024 * 1024)  # several TCP reads each way
        ws.send(big)
        assert ws.recv(timeout=10) == big


def test_handshake_headers(proxy_port: int, upstream: _Upstream) -> None:
    with connect(f"ws://127.0.0.1:{proxy_port}/socket", additional_headers=_ROUTER_HEADERS) as ws:
        ws.send("ping")
        ws.recv(timeout=5)
    headers = upstream.handshakes[-1]
    # Jellyfin sees the public host, as on the HTTP path, and never the router's owner assertion.
    assert headers["Host"] == "jellyfin.zone.example"
    assert "X-OpenHost-Is-Owner" not in headers
    assert headers["Upgrade"].lower() == "websocket"
    assert "Sec-WebSocket-Key" in headers


def test_upstream_refusal_is_passed_through(proxy_port: int) -> None:
    with pytest.raises(InvalidStatus) as excinfo:
        connect(f"ws://127.0.0.1:{proxy_port}/other", open_timeout=15)
    assert excinfo.value.response.status_code == 401


def test_server_close_reaches_client(proxy_port: int) -> None:
    with connect(f"ws://127.0.0.1:{proxy_port}/socket") as ws:
        ws.send("close-from-server")
        with pytest.raises(ConnectionClosed) as excinfo:
            ws.recv(timeout=5)
    assert excinfo.value.rcvd is not None and excinfo.value.rcvd.code == 4000


def test_client_close_reaches_server(proxy_port: int, upstream: _Upstream) -> None:
    with connect(f"ws://127.0.0.1:{proxy_port}/socket") as ws:
        ws.send("hi")
        ws.recv(timeout=5)
    assert upstream.closed.wait(5), "upstream never saw the client's close"


def test_concurrent_tunnels_are_independent(proxy_port: int) -> None:
    with ExitStack() as stack:
        sockets = [stack.enter_context(connect(f"ws://127.0.0.1:{proxy_port}/socket")) for _ in range(5)]
        for i, ws in enumerate(sockets):
            ws.send(f"m{i}")
        assert [ws.recv(timeout=5) for ws in sockets] == [f"m{i}" for i in range(5)]


def test_upstream_down_is_502(auth_proxy: ModuleType) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    server, port = _start_proxy(auth_proxy, dead_port)
    try:
        # Raw socket: BaseHTTPRequestHandler errors are HTTP/1.0, which the websockets client won't parse.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(_raw_upgrade("/socket"))
            assert s.recv(4096).startswith(b"HTTP/1.0 502")
    finally:
        server.shutdown()
        server.server_close()


def test_plain_requests_still_proxied(auth_proxy: ModuleType) -> None:
    """A non-upgrade request keeps going through the normal HTTP path."""
    import http.server

    class Plain(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = b"plain ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    plain = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Plain)
    threading.Thread(target=plain.serve_forever, daemon=True).start()
    server, port = _start_proxy(auth_proxy, plain.server_address[1])
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /System/Info/Public HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = b""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (chunk := s.recv(4096)):
                data += chunk
        assert data.startswith(b"HTTP/1.0 200") and data.endswith(b"plain ok"), data
    finally:
        server.shutdown()
        server.server_close()
        plain.shutdown()
