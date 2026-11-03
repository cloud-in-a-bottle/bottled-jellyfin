"""OpenHost auto-login auth-proxy for Jellyfin.

Sits between the OpenHost router and Jellyfin (.NET on
127.0.0.1:8096). When an authenticated zone owner visits ``/`` without a
current seed cookie, the proxy mints a browser-specific Jellyfin device token
on loopback and serves an HTML shim that:

  1. Writes a ``Credentials`` object containing that AccessToken,
      UserId, server Id, and address into ``localStorage.
      jellyfin_credentials``,
  2. Sets a cookie ``openhost_jellyfin_seeded=<bootstrap-marker>`` so subsequent
      requests don't re-trigger the shim,
  3. ``window.location.replace('/web/')`` to the SPA proper.

Jellyfin's auth model is API-key based: the SPA reads
``localStorage.jellyfin_credentials`` and sets
``Authorization: MediaBrowser Token="..."`` on every API call.
There is no server-side session cookie to set, so a pure 302-with-
Set-Cookie pattern doesn't work — we MUST run JS in the browser to
prime localStorage.

Pattern B (auto-login sidecar) per the OpenHost SSO playbook,
adapted to the localStorage-cookie hybrid Jellyfin uses.

The shim is served only on owner navigations (X-OpenHost-Is-Owner=
true) that target an HTML path we recognise as a "first navigation"
hook, AND when the seeded-cookie isn't present.  Mobile and
desktop Jellyfin clients carry their own credentials and never get
shimmed.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import AbstractSet, Iterable

OWNER_HEADER_NAME = "X-OpenHost-Is-Owner"
USER_HEADER_NAME = "X-OpenHost-User"

# Cookie the shim sets right before redirecting to /web/.  We use
# its presence as a "the owner has been seeded on this browser"
# marker so we don't keep re-running the shim on every navigation
# back to /.  The cookie is per-browser, not persistent across
# revoked tokens — if the operator replaces the bootstrap token after a
# supported rotation, we want the shim to re-fire. We do
# that by encoding the token's last-byte marker in the cookie.
SEEDED_COOKIE_NAME = "openhost_jellyfin_seeded"

# Bumped whenever the seeded credentials change shape, so browsers
# seeded by an older shim get re-seeded on their next visit to /.
# v3: give each browser its own Jellyfin token/device so remote controls target one session.
# v2: LastConnectionMode=Manual (v1 left jellyfin-web stuck on its loading screen).
SHIM_VERSION = "3"

HOP_BY_HOP_HEADERS = frozenset(
    h.lower()
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Host",
        "Content-Length",
    )
)
# We recalculate request Content-Length above, but must preserve the upstream response's
# Content-Length for ranges, HEAD, and detecting an interrupted media download.
RESPONSE_STRIP_HEADERS = HOP_BY_HOP_HEADERS - {"content-length"}

ALWAYS_STRIP_HEADERS = frozenset(
    h.lower() for h in (
        OWNER_HEADER_NAME,
        USER_HEADER_NAME,
    )
)

CLIENT_READ_TIMEOUT_SECONDS = 60
# Bounds connecting to Jellyfin and reading its handshake response for
# a WebSocket upgrade; an open tunnel has no timeout.
WS_HANDSHAKE_TIMEOUT_SECONDS = 10
WS_MAX_RESPONSE_HEAD_BYTES = 64 * 1024
MAX_BODY_BYTES = 256 * 1024 * 1024  # Limit request bodies; stream responses without a size cap.
RESPONSE_CHUNK_BYTES = 64 * 1024
MEDIA_PATH_PREFIXES = ("/items/", "/videos/", "/video/", "/audio/")
MEDIA_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MEDIA_ITEM_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")

logging.basicConfig(
    level=os.environ.get("AUTH_PROXY_LOG_LEVEL", "INFO"),
    format="[auth-proxy] %(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("auth_proxy")


def _parse_cookie_header(cookie_header: str | None) -> dict[str, str]:
    if not cookie_header:
        return {}
    result: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        result.setdefault(name.strip(), value.strip())
    return result


def _strip_headers(
    headers: Iterable[tuple[str, str]], drop: AbstractSet[str]
) -> list[tuple[str, str]]:
    drop_lower = {h.lower() for h in drop}
    return [(k, v) for k, v in headers if k.lower() not in drop_lower]


def _read_admin_token(token_file: str) -> dict | None:
    """Read the bootstrap-generated admin token JSON.

    Format (written by bootstrap_admin.py):
      {
        "server_id": "...",
        "server_name": "...",
        "user_id": "...",
        "access_token": "..."
      }

    Returns None if the file is missing/unreadable; caller falls
    through and the visitor sees Jellyfin's own login page (the
    failure UX is "manual login still works" not "broken").
    """
    try:
        with open(token_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return None
    required = ("server_id", "user_id", "access_token")
    if not all(k in data and isinstance(data[k], str) and data[k] for k in required):
        return None
    return data


def _seeded_marker(token: str) -> str:
    """Per-token marker used as the seeded-cookie value.  When the
    operator rotates the access token (delete admin-token.json),
    the marker changes and the shim re-fires for any browser that
    cached the old cookie.  We expose only the last 12 chars of
    the token, which is well below brute-force-recoverability for
    the full token.  Prefixed with SHIM_VERSION so a shim change
    re-seeds browsers holding credentials from an older shim.
    """
    return f"v{SHIM_VERSION}.{token[-12:]}"


# jellyfin-web's ConnectionMode enum: which address in a Servers
# entry the client connects to.
CONNECTION_MODE_MANUAL = 2


def _build_shim_html(creds: dict) -> bytes:
    """Render the localStorage-priming HTML page.

    The page POSTs nothing — all state comes from JS-templated
    JSON literals.  We deliberately don't fetch a separate JSON
    document because that would require additional public_paths
    plumbing.
    """
    server_id = creds["server_id"]
    server_name = creds.get("server_name", "Jellyfin")
    user_id = creds["user_id"]
    access_token = creds["access_token"]
    # The cookie tracks the bootstrap token, not the per-browser token. Otherwise a fresh
    # token would change the marker on every navigation and mint tokens indefinitely.
    seeded_value = creds.get("seed_marker") or _seeded_marker(access_token)

    # We use json.dumps to safely escape strings into JS string
    # literals; never do raw f-string interpolation of user-
    # controlled values into JS code.
    creds_payload = {
        "Servers": [
            {
                "Id": server_id,
                "Name": server_name,
                "AccessToken": access_token,
                "UserId": user_id,
                # ManualAddress and RemoteAddress are filled in by
                # JS from window.location.origin, so the scheme and
                # port match however the browser reached the app
                # (https on public zones, plain http on .local).
                "ManualAddress": "",
                "RemoteAddress": "",
                # The client connects to the address for this mode.
                # Manual -> ManualAddress.  Local (0) would need a
                # LocalAddress, which we have no use for.
                "LastConnectionMode": CONNECTION_MODE_MANUAL,
                "DateLastAccessed": 0,  # filled in by JS
                "Type": "Server",
            }
        ]
    }

    payload_js = json.dumps(creds_payload)
    seeded_js = json.dumps(seeded_value)
    web_url = "/web/"

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Signing in to Jellyfin via OpenHost SSO…</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      font-family: -apple-system, system-ui, sans-serif;
      display:flex; min-height:100vh; align-items:center;
      justify-content:center; background:#101820; color:#e6e6e6;
      margin:0;
    }}
    .card {{
      text-align:center; padding:2em 3em; background:#1a2632;
      border:1px solid #243345; border-radius:8px;
      max-width:32em;
    }}
    .spinner {{
      width:32px; height:32px; border:4px solid #2c3e50;
      border-top-color:#00a4dc; border-radius:50%;
      animation:spin 1s linear infinite;
      margin:0 auto 1em;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    a {{ color:#00a4dc; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <p>Signing in to Jellyfin via OpenHost SSO…</p>
    <p><small>If you aren't redirected,
       <a id="manual" href="{web_url}">click here</a>.</small></p>
  </div>
  <script>
  (function () {{
    var creds = {payload_js};
    var server = creds.Servers[0];
    server.ManualAddress = window.location.origin;
    server.RemoteAddress = window.location.origin;
    server.DateLastAccessed = Date.now();
    try {{
      localStorage.setItem('jellyfin_credentials', JSON.stringify(creds));
    }} catch (e) {{
      // Disabled localStorage: fall through to /web/ where the
      // user sees the standard login form.  Worse UX than auto-
      // login, but still functional.
      console.error('openhost-jellyfin: localStorage unavailable: ' + e);
    }}
    // Mark this browser as seeded so subsequent visits don't re-
    // shim.  Path=/ so it covers all of Jellyfin's URLs; SameSite
    // Lax so it survives top-level navigations.  Secure only over
    // https: browsers drop Secure cookies set from plain http, which
    // would re-run the shim on every visit to a .local zone.
    document.cookie = 'openhost_jellyfin_seeded=' + {seeded_js}
      + '; Path=/; Max-Age=31536000; SameSite=Lax'
      + (window.location.protocol === 'https:' ? '; Secure' : '');
    window.location.replace('{web_url}');
  }})();
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


class AuthProxyHandler(BaseHTTPRequestHandler):
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 8096
    token_file: str = "/data/app_data/jellyfin/admin-token.json"

    def log_message(self, format: str, *args) -> None:  # noqa: A002, N802
        # Media URLs and /socket frequently carry api_key query parameters. BaseHTTPRequestHandler's
        # default request-line log would persist those access tokens verbatim.
        status = args[1] if format == '"%s" %s %s' and len(args) >= 2 else "-"
        log.info("%s %s -> %s", self.command, self.path.split("?", 1)[0], status)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def _safe_send_error(self, code: int, message: str) -> None:
        try:
            self.send_error(code, message)
        except OSError as exc:
            log.debug("client disconnected before error response: %s", exc)

    def _dispatch(self) -> None:
        try:
            self.connection.settimeout(CLIENT_READ_TIMEOUT_SECONDS)
        except OSError:
            pass

        if self.path == "/_healthz":
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "3")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(b"ok\n")
            except OSError:
                pass
            return

        is_owner = self.headers.get(OWNER_HEADER_NAME, "").lower() == "true"
        cookies = _parse_cookie_header(self.headers.get("Cookie"))

        accept = self.headers.get("Accept", "")
        is_html_navigation = (
            self.command == "GET" and "text/html" in accept.lower()
        )

        # Routes that should NEVER be shimmed even on owner HTML
        # navigations: anything inside /web/ (the SPA itself) so we
        # only intercept the top-level / and /index.html, NEVER any
        # subpath that the SPA's own router uses.  Also Jellyfin
        # API surface.
        is_pre_spa = self.path in ("", "/", "/index.html") or self.path.startswith("/?")

        creds = None
        if is_owner and is_html_navigation and is_pre_spa:
            creds = _read_admin_token(self.token_file)
            if creds is None:
                log.debug(
                    "auto-shim: token file missing/unreadable; falling "
                    "through to upstream login page"
                )
            else:
                expected_marker = _seeded_marker(creds["access_token"])
                already_seeded = (
                    cookies.get(SEEDED_COOKIE_NAME) == expected_marker
                )
                if not already_seeded:
                    self._serve_shim(self._browser_creds(creds))
                    return

        if not is_owner and self._is_media_path() and not self._has_valid_media_token():
            self._safe_send_error(401, "Jellyfin authentication required")
            return

        if self._is_websocket_upgrade():
            self._proxy_websocket()
            return
        self._proxy()

    def _decoded_media_path(self) -> str | None:
        path = urllib.parse.urlsplit(self.path).path
        # The router accepts unambiguous percent-encoded public paths and the .NET
        # backend decodes them. Check each decoded form so encoding cannot bypass auth.
        for _ in range(8):
            if path.lower().startswith(MEDIA_PATH_PREFIXES):
                return path
            decoded = urllib.parse.unquote(path)
            if decoded == path:
                break
            path = decoded
        return None

    def _is_media_path(self) -> bool:
        return self._decoded_media_path() is not None

    def _has_valid_media_token(self) -> bool:
        """Jellyfin's stream handlers allow anonymous requests, so authenticate before proxying.

        Browser media requests include an Authorization header; mobile clients and shared
        links may instead put api_key in the URL. Check with Jellyfin itself rather than
        treating the mere presence of a token as proof of authorization.
        """
        authorization = self.headers.get("Authorization") or self.headers.get("X-Emby-Authorization")
        if not authorization:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            candidates = [value for key, values in query.items() if key.lower() in ("api_key", "apikey") for value in values]
            token = candidates[0] if len(candidates) == 1 else ""
            if not MEDIA_TOKEN_PATTERN.fullmatch(token):
                return False
            authorization = f'MediaBrowser Token="{token}"'

        path = self._decoded_media_path()
        item_id = path.split("/", 3)[2] if path else ""
        if not MEDIA_ITEM_ID_PATTERN.fullmatch(item_id):
            return False

        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=10)
        try:
            conn.request("GET", "/Users/Me", headers={"Authorization": authorization})
            response = conn.getresponse()
            if response.status != 200:
                return False
            user = json.loads(response.read(64 * 1024 + 1))
            user_id = user.get("Id")
            if not isinstance(user_id, str) or not MEDIA_ITEM_ID_PATTERN.fullmatch(user_id):
                return False
            # Jellyfin stream routes allow tokenless playback. /Items/{id} applies the
            # user's library policy, so check it before handing out any media bytes.
            conn.request(
                "GET", f"/Items/{item_id}?userId={user_id}", headers={"Authorization": authorization}
            )
            item = conn.getresponse()
            return item.status == 200
        except (OSError, ValueError, http.client.HTTPException) as exc:
            log.warning("media token validation failed: %s", type(exc).__name__)
            return False
        finally:
            conn.close()

    def _browser_creds(self, bootstrap: dict) -> dict:
        """Mint a distinct Jellyfin session for this owner's browser on its first visit.

        The password never leaves the container. If an older token file lacks it, or Jellyfin
        is temporarily unavailable, the existing bootstrap token still provides owner SSO.
        """
        username, password = bootstrap.get("username"), bootstrap.get("password")
        if not username or not password:
            return bootstrap
        device_id = "openhost-browser-" + secrets.token_urlsafe(18)
        identity = (
            'MediaBrowser Client="Cloud in a Bottle", Device="Browser", '
            f'DeviceId="{device_id}", Version="1"'
        )
        body = json.dumps({"Username": username, "Pw": password}).encode()
        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=10)
        try:
            conn.request(
                "POST", "/Users/AuthenticateByName", body=body,
                headers={"Authorization": identity, "X-Emby-Authorization": identity,
                         "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            result = response.read(64 * 1024 + 1)
            if response.status != 200 or len(result) > 64 * 1024:
                log.warning("browser token mint returned HTTP %d", response.status)
                return bootstrap
            parsed = json.loads(result)
            token = parsed.get("AccessToken")
            user_id = (parsed.get("User") or {}).get("Id")
            if not isinstance(token, str) or not token or user_id != bootstrap["user_id"]:
                log.warning("browser token mint returned incomplete credentials")
                return bootstrap
            return {**bootstrap, "access_token": token, "seed_marker": _seeded_marker(bootstrap["access_token"])}
        except (OSError, ValueError, http.client.HTTPException) as exc:
            log.warning("browser token mint failed: %s", type(exc).__name__)
            return bootstrap
        finally:
            conn.close()

    def _serve_shim(self, creds: dict) -> None:
        try:
            page = _build_shim_html(creds)
        except (KeyError, ValueError) as exc:
            log.warning("auto-shim: failed to render shim HTML: %s", exc)
            self._proxy()
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(page)
        except OSError as exc:
            log.debug("client disconnected during shim response: %s", exc)
        log.info("auto-shim: served Jellyfin localStorage shim")

    def _is_websocket_upgrade(self) -> bool:
        connection_tokens = {
            t.strip().lower() for t in self.headers.get("Connection", "").split(",")
        }
        return (
            self.command == "GET"
            and self.headers.get("Upgrade", "").strip().lower() == "websocket"
            and "upgrade" in connection_tokens
        )

    def _proxy_websocket(self) -> None:
        """Tunnel a WebSocket upgrade (Jellyfin's /socket) to upstream.

        http.client can't carry a 101 Switching Protocols, and
        _proxy() strips Upgrade/Connection as hop-by-hop, so upgrades
        are forwarded over a raw socket instead: send the handshake,
        relay upstream's response head verbatim, then pipe bytes both
        ways until either side closes.  Frames are not parsed.
        """
        # The connection is dedicated to the tunnel from here on.
        self.close_connection = True
        headers = _strip_headers(
            self.headers.items(),
            HOP_BY_HOP_HEADERS | ALWAYS_STRIP_HEADERS,
        )
        forwarded_host = self.headers.get("X-Forwarded-Host", "").strip()
        host = forwarded_host or self.headers.get("Host", "").strip() or "localhost"
        head = [f"{self.command} {self.path} HTTP/1.1", f"Host: {host}"]
        head += [f"{k}: {v}" for k, v in headers]
        head += ["Connection: Upgrade", "Upgrade: websocket", "", ""]
        try:
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port),
                timeout=WS_HANDSHAKE_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            log.warning("websocket upstream connect error: %s", exc)
            self._safe_send_error(502, "Bad Gateway")
            return
        try:
            try:
                upstream.sendall("\r\n".join(head).encode("latin-1"))
                response_head = b""
                while b"\r\n\r\n" not in response_head:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        raise ConnectionError("upstream closed during handshake")
                    response_head += chunk
                    if len(response_head) > WS_MAX_RESPONSE_HEAD_BYTES:
                        raise ConnectionError("upstream handshake response too large")
            except (OSError, UnicodeEncodeError) as exc:
                log.warning("websocket upstream handshake error: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                return
            try:
                # Relay the response (and any frames already read
                # past it) as-is: a 101 carries the Upgrade and
                # Sec-WebSocket-Accept headers the client checks, and
                # a refusal (eg 401) is passed through for the client.
                self.connection.sendall(response_head)
            except OSError as exc:
                log.debug("client disconnected during websocket handshake: %s", exc)
                return
            status_line = response_head.split(b"\r\n", 1)[0]
            if b" 101 " not in status_line + b" ":
                # Not upgraded: relay the rest of the response body.
                upstream.settimeout(WS_HANDSHAKE_TIMEOUT_SECONDS)
                self._pipe(upstream, self.connection)
                return
            # Long-lived from here: Jellyfin and its clients exchange
            # keepalives, so idle gaps don't need a timeout.
            upstream.settimeout(None)
            self.connection.settimeout(None)
            log.info("websocket tunnel open: %s", self.path.split("?", 1)[0])
            to_client = threading.Thread(
                target=self._pipe, args=(upstream, self.connection), daemon=True
            )
            to_client.start()
            self._pipe_client_to(upstream)
            to_client.join(timeout=5)
            log.info("websocket tunnel closed: %s", self.path.split("?", 1)[0])
        finally:
            for sock in (upstream, self.connection):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            upstream.close()

    def _pipe_client_to(self, dest: socket.socket) -> None:
        """Client -> upstream.  Reads through rfile so any bytes it
        already buffered past the request head aren't lost."""
        try:
            while True:
                data = self.rfile.read1(65536)
                if not data:
                    break
                dest.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            # Unblock the other direction.
            try:
                dest.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @staticmethod
    def _pipe(src: socket.socket, dest: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dest.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dest.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _proxy(self) -> None:
        cleaned_headers = _strip_headers(
            self.headers.items(),
            HOP_BY_HOP_HEADERS | ALWAYS_STRIP_HEADERS,
        )
        forwarded_host = self.headers.get("X-Forwarded-Host", "").strip()
        if forwarded_host:
            cleaned_headers.append(("Host", forwarded_host))

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower().strip()
        if transfer_encoding and transfer_encoding != "identity":
            self._safe_send_error(501, "Transfer-Encoding not supported")
            return

        body: bytes | None = None
        content_length_header = self.headers.get("Content-Length")
        if content_length_header:
            try:
                length = int(content_length_header)
            except ValueError:
                self._safe_send_error(400, "invalid Content-Length")
                return
            if length < 0:
                self._safe_send_error(400, "negative Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._safe_send_error(413, "request body too large")
                return
            if length > 0:
                try:
                    body = self.rfile.read(length)
                except (OSError, TimeoutError) as exc:
                    log.info("client read error: %s", exc)
                    self._safe_send_error(400, "request body read failed")
                    return
                if len(body) != length:
                    self._safe_send_error(400, "incomplete request body")
                    return
            else:
                body = b""
        elif self.command in ("POST", "PUT", "PATCH", "DELETE"):
            body = b""

        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=300
        )
        try:
            try:
                conn.putrequest(
                    self.command,
                    self.path,
                    skip_host=True,
                    skip_accept_encoding=True,
                )
                for key, value in cleaned_headers:
                    conn.putheader(key, value)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(message_body=body)
                upstream = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                log.warning("upstream error: %s", exc)
                self._safe_send_error(502, "Bad Gateway")
                return

            reason = upstream.reason or ""
            try:
                self.send_response(upstream.status, reason)
                for key, value in upstream.getheaders():
                    if key.lower() in RESPONSE_STRIP_HEADERS:
                        continue
                    self.send_header(key, value)
                # http.client decodes upstream chunked transfer encoding. An unknown-length
                # response is delimited by closing this HTTP/1.0 hop, not by copying its TE.
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if self.command != "HEAD":
                    while chunk := upstream.read1(RESPONSE_CHUNK_BYTES):
                        self.wfile.write(chunk)
            except OSError as exc:
                # Once headers have been sent a 502 cannot replace the response; close the
                # connection so a client with Content-Length can detect the truncation.
                log.debug("stream interrupted or client disconnected: %s", exc)
            except http.client.HTTPException as exc:
                log.warning("upstream stream interrupted: %s", exc)
            finally:
                upstream.close()
        finally:
            conn.close()


class IPv4ThreadingServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = True
    daemon_threads = True


def _port_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer: {exc}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name}={raw!r} is out of range (1-65535)")
    return port


def main() -> int:
    try:
        listen_port = _port_from_env("AUTH_PROXY_LISTEN_PORT", 8080)
        upstream_port = _port_from_env("AUTH_PROXY_UPSTREAM_PORT", 8096)
    except ValueError as exc:
        log.error("invalid port configuration: %s", exc)
        return 1

    upstream_host = os.environ.get("AUTH_PROXY_UPSTREAM_HOST", "127.0.0.1").strip()
    token_file = os.environ.get(
        "AUTH_PROXY_TOKEN_FILE",
        "/data/app_data/jellyfin/admin-token.json",
    )

    AuthProxyHandler.upstream_host = upstream_host
    AuthProxyHandler.upstream_port = upstream_port
    AuthProxyHandler.token_file = token_file

    try:
        server = IPv4ThreadingServer(("0.0.0.0", listen_port), AuthProxyHandler)
    except OSError as exc:
        log.error(
            "failed to bind auth-proxy listener on 0.0.0.0:%d: %s",
            listen_port,
            exc,
        )
        return 1
    log.info(
        "listening on 0.0.0.0:%d -> %s:%d (token=%s)",
        listen_port,
        upstream_host,
        upstream_port,
        token_file,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
