"""bootstrap_admin.py must ride out Jellyfin's setup-server -> server handover on first boot.

Jellyfin 12.x first serves a temporary setup server (which already answers /System/Info/Public), then stops it and
starts the real server a few seconds later. A bootstrap request landing in that gap was refused and killed the
bootstrap, so no admin token was written and the owner got Jellyfin's setup wizard instead of SSO.
"""

import importlib.util
import socket
import threading
import time
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest

_BOOTSTRAP = Path(__file__).resolve().parent.parent / "bootstrap_admin.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def bootstrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("JELLYFIN_PORT", str(_free_port()))
    monkeypatch.setenv("TOKEN_FILE", str(tmp_path / "admin-token.json"))
    spec = importlib.util.spec_from_file_location("bootstrap_admin", _BOOTSTRAP)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Handler(BaseHTTPRequestHandler):
    posts: ClassVar[list[str]] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(404 if self.path == "/missing" else 200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self) -> None:
        _Handler.posts.append(self.path)
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(204)
        self.end_headers()


@pytest.fixture
def late_server(bootstrap: ModuleType) -> Iterator[None]:
    """Start listening on Jellyfin's port only after a gap, like the setup-server -> server handover."""
    _Handler.posts = []
    servers: list[ThreadingHTTPServer] = []

    def start() -> None:
        time.sleep(2)
        server = ThreadingHTTPServer(("127.0.0.1", bootstrap.JELLYFIN_PORT), _Handler)
        servers.append(server)
        server.serve_forever()

    threading.Thread(target=start, daemon=True).start()
    yield
    for server in servers:
        server.shutdown()


def test_post_waits_out_refused_connection(bootstrap: ModuleType, late_server: None) -> None:
    status, _, _ = bootstrap._request("POST", "/Startup/Configuration", body={"UICulture": "en-US"})
    assert status == 204
    assert _Handler.posts == ["/Startup/Configuration"], "a refused POST must be sent exactly once when retried"


def test_http_errors_are_not_retried(bootstrap: ModuleType, late_server: None) -> None:
    bootstrap._request("GET", "/")  # wait for the server
    started = time.monotonic()
    status, _, _ = bootstrap._request("GET", "/missing")
    assert status == 404
    assert time.monotonic() - started < 1


def test_gives_up_after_the_gap(bootstrap: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "RESTART_GAP_SECONDS", 1)
    with pytest.raises(urllib.error.URLError):
        bootstrap._request("GET", "/System/Info/Public")
