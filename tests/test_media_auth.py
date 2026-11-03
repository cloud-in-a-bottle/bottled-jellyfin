"""Public media routes require a real Jellyfin token unless the router authenticated the owner."""

import importlib.util
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

ITEM_ID = "a" * 32
USER_ID = "b" * 32


@pytest.fixture(scope="module")
def proxy_module() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "auth_proxy.py"
    spec = importlib.util.spec_from_file_location("auth_proxy_media", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def proxy_url(proxy_module: ModuleType) -> Iterator[str]:
    class Media(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/Users/Me":
                allowed = self.headers.get("Authorization") in (
                    'MediaBrowser Token="valid"', 'MediaBrowser Token="restricted"'
                )
                body = ('{"Id":"' + USER_ID + '"}').encode() if allowed else b"Unauthorized"
                self.send_response(200 if allowed else 401)
            elif self.path == f"/Items/{ITEM_ID}?userId={USER_ID}":
                allowed = self.headers.get("Authorization") == 'MediaBrowser Token="valid"'
                body = b"item" if allowed else b"Not found"
                self.send_response(200 if allowed else 404)
            else:
                body = b"private media"
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Media)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    handler = type(
        "Proxy", (proxy_module.AuthProxyHandler,),
        {"upstream_host": "127.0.0.1", "upstream_port": upstream.server_address[1], "token_file": "/nonexistent"},
    )
    proxy = proxy_module.IPv4ThreadingServer(("127.0.0.1", 0), handler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{proxy.server_address[1]}"
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


@pytest.mark.parametrize("path", [
    f"/Videos/{ITEM_ID}/stream.mp4", f"/videos/{ITEM_ID}/stream.mp4",
    f"/Audio/{ITEM_ID}/stream.mp3", f"/audio/{ITEM_ID}/stream.mp3",
    f"/Items/{ITEM_ID}/Download", f"/%56ideos/{ITEM_ID}/stream.mp4",
])
def test_public_media_needs_valid_jellyfin_auth(proxy_url: str, path: str) -> None:
    assert _get(proxy_url + path)[0] == 401
    assert _get(proxy_url + path, {"Authorization": 'MediaBrowser Token="bogus"'})[0] == 401
    assert _get(proxy_url + path, {"Authorization": 'MediaBrowser Token="valid"'}) == (200, b"private media")
    assert _get(proxy_url + path, {"Authorization": 'MediaBrowser Token="restricted"'})[0] == 401


def test_query_token_is_checked_but_never_logged(proxy_url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="auth_proxy"):
        assert _get(proxy_url + f"/Videos/{ITEM_ID}/stream.mp4?ApiKey=valid") == (200, b"private media")
    assert "ApiKey" not in caplog.text
    assert "valid" not in caplog.text
    assert _get(proxy_url + f"/Videos/{ITEM_ID}/stream.mp4?ApiKey=bogus")[0] == 401


def test_authenticated_owner_needs_no_separate_jellyfin_token(proxy_url: str) -> None:
    assert _get(proxy_url + f"/Videos/{ITEM_ID}/stream.mp4", {"X-OpenHost-Is-Owner": "true"}) == (200, b"private media")
