"""Owner SSO must mint a separate Jellyfin device for each browser session."""

import http.client
import importlib.util
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

_CREDS = re.compile(rb"var creds = (\{.*\});")


@pytest.fixture(scope="module")
def proxy_module() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "auth_proxy.py"
    spec = importlib.util.spec_from_file_location("auth_proxy_sessions", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def proxy_pair(proxy_module: ModuleType, tmp_path: Path) -> Iterator[tuple[int, list[str], Path]]:
    file = tmp_path / "admin-token.json"
    file.write_text(json.dumps({"access_token": "bootstrap-token", "user_id": "admin-id", "server_id": "server-id", "username": "owner", "password": "private-password"}))
    devices: list[str] = []

    class Jellyfin(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            assert self.path == "/Users/AuthenticateByName"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body == {"Username": "owner", "Pw": "private-password"}
            identity = self.headers["Authorization"]
            assert self.headers["X-Emby-Authorization"] == identity
            device = re.search(r'DeviceId="([^"]+)"', identity)
            assert device is not None
            devices.append(device.group(1))
            reply = json.dumps({"AccessToken": f"browser-token-{len(devices)}", "User": {"Id": "admin-id"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "/web/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Jellyfin)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    handler = type("Proxy", (proxy_module.AuthProxyHandler,), {"upstream_host": "127.0.0.1", "upstream_port": upstream.server_address[1], "token_file": str(file)})
    proxy = proxy_module.IPv4ThreadingServer(("127.0.0.1", 0), handler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        yield proxy.server_address[1], devices, file
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()


def _owner_get(port: int, cookie: str = "") -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/", headers={"X-OpenHost-Is-Owner": "true", "Accept": "text/html", "Cookie": cookie})
        reply = conn.getresponse()
        return reply.status, reply.read()
    finally:
        conn.close()


def test_each_browser_gets_its_own_token_and_stable_seed_marker(
    proxy_pair: tuple[int, list[str], Path], proxy_module: ModuleType
) -> None:
    port, devices, _ = proxy_pair
    tokens = []
    for _ in range(2):
        status, page = _owner_get(port)
        assert status == 200
        match = _CREDS.search(page)
        assert match is not None
        tokens.append(json.loads(match.group(1))["Servers"][0]["AccessToken"])
        assert b"private-password" not in page
        assert f"document.cookie = 'openhost_jellyfin_seeded=' + {json.dumps(proxy_module._seeded_marker('bootstrap-token'))}".encode() in page
    assert tokens == ["browser-token-1", "browser-token-2"]
    assert len(set(devices)) == 2

    # The marker is based on the bootstrap token, not the browser token. A later
    # visit must go straight through, without minting another session.
    marker = proxy_module._seeded_marker("bootstrap-token")
    status, _ = _owner_get(port, f"openhost_jellyfin_seeded={marker}")
    assert status == 302
    assert len(devices) == 2


def test_older_token_file_without_password_still_allows_sso(proxy_pair: tuple[int, list[str], Path]) -> None:
    port, devices, file = proxy_pair
    file.write_text(json.dumps({"access_token": "bootstrap-token", "user_id": "admin-id", "server_id": "server-id"}))
    status, page = _owner_get(port)
    assert status == 200
    match = _CREDS.search(page)
    assert match is not None
    assert json.loads(match.group(1))["Servers"][0]["AccessToken"] == "bootstrap-token"
    assert devices == []


def test_browser_with_old_shared_token_is_reseeded(proxy_pair: tuple[int, list[str], Path]) -> None:
    port, devices, _ = proxy_pair
    status, page = _owner_get(port, "openhost_jellyfin_seeded=v2.tstrap-token")
    assert status == 200
    match = _CREDS.search(page)
    assert match is not None
    assert json.loads(match.group(1))["Servers"][0]["AccessToken"] == "browser-token-1"
    assert len(devices) == 1
