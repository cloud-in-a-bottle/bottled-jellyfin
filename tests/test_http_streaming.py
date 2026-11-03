"""Media responses must stream through the auth proxy, including ranges and chunked replies."""

import importlib.util
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def proxy_module() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "auth_proxy.py"
    spec = importlib.util.spec_from_file_location("auth_proxy_streaming", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def proxy_pair(proxy_module: ModuleType) -> Iterator[tuple[str, threading.Event]]:
    finish = threading.Event()

    class Media(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/slow":
                self.send_response(200)
                self.send_header("Content-Length", "10")
                self.end_headers()
                self.wfile.write(b"first")
                finish.wait(8)
                self.wfile.write(b"later")
                return
            if self.path == "/chunked":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n")
                return
            if self.headers.get("Range") == "bytes=0-3":
                self.send_response(206)
                self.send_header("Content-Range", "bytes 0-3/4096")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"medi")
                return
            self.send_response(200)
            self.send_header("Content-Length", "4096")
            self.end_headers()
            self.wfile.write(b"media" + b"x" * 4091)

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "4096")
            self.end_headers()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Media)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    handler = type(
        "ProxyHandler",
        (proxy_module.AuthProxyHandler,),
        {"upstream_host": "127.0.0.1", "upstream_port": upstream.server_address[1], "token_file": "/nonexistent"},
    )
    proxy = proxy_module.IPv4ThreadingServer(("127.0.0.1", 0), handler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{proxy.server_address[1]}", finish
    finally:
        finish.set()
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_large_media_is_not_capped_by_request_body_limit(
    proxy_pair: tuple[str, threading.Event], proxy_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_module, "MAX_BODY_BYTES", 1024)
    with urllib.request.urlopen(proxy_pair[0] + "/large", timeout=10) as response:
        assert response.status == 200
        assert response.headers["Content-Length"] == "4096"
        assert response.read() == b"media" + b"x" * 4091


def test_first_media_bytes_arrive_before_upstream_finishes(proxy_pair: tuple[str, threading.Event]) -> None:
    url, finish = proxy_pair
    with urllib.request.urlopen(url + "/slow", timeout=3) as response:
        assert response.status == 200
        assert response.read(5) == b"first"
        finish.set()
        assert response.read() == b"later"


def test_chunked_response_is_decoded_and_delimited_by_close(proxy_pair: tuple[str, threading.Event]) -> None:
    with urllib.request.urlopen(proxy_pair[0] + "/chunked", timeout=10) as response:
        assert response.status == 200
        assert response.headers.get("Transfer-Encoding") is None
        assert response.headers["Connection"] == "close"
        assert response.read() == b"helloworld"


def test_byte_range_and_head_keep_their_semantics(proxy_pair: tuple[str, threading.Event]) -> None:
    url, _ = proxy_pair
    request = urllib.request.Request(url + "/large", headers={"Range": "bytes=0-3"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 0-3/4096"
        assert response.read() == b"medi"
    request = urllib.request.Request(url + "/large", method="HEAD")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        assert response.headers["Content-Length"] == "4096"
        assert response.read() == b""
