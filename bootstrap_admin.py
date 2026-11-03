#!/usr/bin/env python3
"""First-boot setup-wizard automation for openhost-jellyfin.

Jellyfin's first run leaves the server in "startup wizard" state:
``/System/Info/Public`` returns ``StartupWizardCompleted: false``,
the wizard endpoints under ``/Startup/*`` are reachable without
auth (policy ``FirstTimeSetupOrElevated``), and the SPA serves a
multi-step setup form.

We walk the wizard programmatically so the operator never sees it:

  1. POST /Startup/Configuration  (server name + UI culture)
  2. POST /Startup/RemoteAccess   (EnableRemoteAccess: true)
  3. POST /Startup/User           (admin username + password)
  4. POST /Startup/Complete

After the wizard is complete, mint a long-lived access token via
``POST /Users/AuthenticateByName`` and persist {server_id, user_id,
access_token, server_name} to ``$TOKEN_FILE`` (mode 0600) for
``auth_proxy.py`` to read on every owner shim invocation.

If ``$TOKEN_FILE`` already exists, the script is a no-op — the
wizard has already been driven and the token persisted across
container restarts.

Errors:
  * Wizard endpoints unreachable / Jellyfin not yet ready → exit 1.
  * Wizard returns 4xx → exit 1.  start.sh propagates the failure
    so the operator sees it rather than a silently-broken
    deployment.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

JELLYFIN_PORT = int(os.environ.get("JELLYFIN_PORT", "8096"))
JELLYFIN = f"http://127.0.0.1:{JELLYFIN_PORT}"
TOKEN_FILE = os.environ.get(
    "TOKEN_FILE", "/data/app_data/jellyfin/admin-token.json"
)
ADMIN_USERNAME = os.environ.get("JELLYFIN_ADMIN_USERNAME", "owner")
SERVER_NAME = os.environ.get(
    "JELLYFIN_SERVER_NAME",
    f"OpenHost ({os.environ.get('OPENHOST_ZONE_DOMAIN', 'jellyfin')})",
)


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(40))


# On first boot Jellyfin (12.x) serves a temporary setup server while
# it migrates the database, then stops it and starts the real one a
# few seconds later.  The setup server already answers
# /System/Info/Public with 200, so _wait_for_jellyfin_ready can
# return just before that gap, and the next request is refused.
# Connection-level failures are retried for this long.
RESTART_GAP_SECONDS = 120


def _urlopen_through_restart(req: urllib.request.Request):
    """urlopen, retrying while nothing is listening (Jellyfin's
    setup-server -> server handover).  Only a refused connection is
    retried: the request never reached Jellyfin, so retrying a POST
    can't apply it twice."""
    deadline = time.monotonic() + RESTART_GAP_SECONDS
    while True:
        try:
            return urllib.request.urlopen(req, timeout=20)
        except urllib.error.URLError as exc:
            refused = isinstance(exc, ConnectionRefusedError) or isinstance(
                getattr(exc, "reason", None), ConnectionRefusedError
            )
            if not refused or time.monotonic() >= deadline:
                raise
            print(f"[bootstrap] {req.get_method()} {req.full_url}: connection refused; retrying")
            time.sleep(1)
        except ConnectionRefusedError:
            if time.monotonic() >= deadline:
                raise
            print(f"[bootstrap] {req.get_method()} {req.full_url}: connection refused; retrying")
            time.sleep(1)


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    expect_json: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    """Send an HTTP request to Jellyfin, returning (status, headers,
    body).  ``body`` is JSON-encoded; the Content-Type is auto-set.
    """
    data: bytes | None = None
    final_headers = {
        "Accept": "application/json",
        # Jellyfin's BodyOnlyHeader middleware requires
        # X-Emby-Authorization on most authenticated requests.
        # During the startup wizard the endpoints are anonymous;
        # we only send X-Emby-Authorization on /Users/AuthenticateByName.
    }
    if headers:
        final_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
        final_headers["Content-Length"] = str(len(data))

    req = urllib.request.Request(
        JELLYFIN + path,
        data=data,
        method=method,
        headers=final_headers,
    )
    try:
        resp = _urlopen_through_restart(req)
        status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        resp_body = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        resp_headers = (
            {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        )
        try:
            resp_body = exc.read()
        except Exception:  # noqa: BLE001
            resp_body = b""
    return status, resp_headers, resp_body


def _wait_for_jellyfin_ready(retries: int = 240, delay: float = 1.0) -> None:
    for i in range(retries):
        try:
            status, _, _ = _request("GET", "/System/Info/Public")
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(delay)
    raise RuntimeError(
        f"Jellyfin /System/Info/Public did not respond 200 within "
        f"{retries * delay:.0f}s"
    )


def _wizard_done() -> bool:
    """Check whether the startup wizard is already complete."""
    status, _, body = _request("GET", "/System/Info/Public")
    if status != 200:
        return False
    try:
        info = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return False
    return bool(info.get("StartupWizardCompleted"))


def _server_id() -> str:
    status, _, body = _request("GET", "/System/Info/Public")
    if status != 200:
        raise RuntimeError(f"/System/Info/Public returned {status}")
    return json.loads(body)["Id"]


def _post_with_retry(path: str, body: dict | None, retries: int = 30, delay: float = 2.0) -> tuple[int, bytes]:
    """POST with retry on 5xx.  Jellyfin reports
    /System/Info/Public ready before the controller layer for
    /Startup/* is wired up — early POSTs return 503 with the
    startup-wizard HTML page instead of routing to the
    StartupController.  Retry a few times to give the .NET host a
    chance to finish bringing controllers online.
    """
    last_status = 0
    last_body = b""
    for _ in range(retries):
        status, _, resp_body = _request("POST", path, body=body)
        if status in (200, 204):
            return status, resp_body
        last_status = status
        last_body = resp_body
        # 503 with HTML body = wizard page, controller not ready;
        # retry.  4xx = real error, give up immediately.
        if status < 500:
            break
        time.sleep(delay)
    raise RuntimeError(
        f"{path} returned {last_status} after retries: {last_body[:200]!r}"
    )


def _drive_wizard(password: str) -> None:
    print("[bootstrap] Step 1/5: POST /Startup/Configuration")
    _post_with_retry(
        "/Startup/Configuration",
        body={
            "UICulture": "en-US",
            "MetadataCountryCode": "US",
            "PreferredMetadataLanguage": "en",
        },
    )

    print("[bootstrap] Step 2/5: POST /Startup/RemoteAccess")
    # Jellyfin's RemoteAccess endpoint also takes EnableAutomaticPortMapping
    # which we want OFF — we don't run UPnP and OpenHost handles inbound
    # routing.
    _post_with_retry(
        "/Startup/RemoteAccess",
        body={
            "EnableRemoteAccess": True,
            "EnableAutomaticPortMapping": False,
        },
    )

    # GET /Startup/User triggers UserManager.InitializeAsync()
    # which creates the default admin user.  Without this prior
    # GET, POST /Startup/User dies with
    # System.InvalidOperationException: Sequence contains no
    # elements (it does Users.First()) on a fresh database.
    # See Jellyfin.Api.Controllers.StartupController.GetFirstUser.
    print("[bootstrap] Step 3/5: GET /Startup/User (initialise default admin)")
    status, _, body = _request("GET", "/Startup/User")
    if status != 200:
        raise RuntimeError(
            f"/Startup/User GET returned {status}: {body[:200]!r}"
        )

    print(f"[bootstrap] Step 4/5: POST /Startup/User (rename to {ADMIN_USERNAME!r})")
    _post_with_retry(
        "/Startup/User",
        body={"Name": ADMIN_USERNAME, "Password": password},
    )

    print("[bootstrap] Step 5/5: POST /Startup/Complete")
    _post_with_retry("/Startup/Complete", body=None)


def _authenticate(username: str, password: str) -> tuple[str, str]:
    """POST /Users/AuthenticateByName and return (user_id,
    access_token).
    """
    # Jellyfin requires X-Emby-Authorization (or
    # Authorization: MediaBrowser ...) describing the client.
    # The wire format is a comma-separated list of
    # key="value" pairs.  Required keys: Client, Device, DeviceId,
    # Version.
    auth_header = (
        'MediaBrowser Client="OpenHost", '
        'Device="OpenHost-Bootstrap", '
        f'DeviceId="openhost-bootstrap-{os.getpid()}", '
        'Version="0.1.0"'
    )
    status, _, body = _request(
        "POST",
        "/Users/AuthenticateByName",
        body={"Username": username, "Pw": password},
        headers={
            "Authorization": auth_header,
            "X-Emby-Authorization": auth_header,
        },
    )
    if status != 200:
        raise RuntimeError(
            f"/Users/AuthenticateByName returned {status}: {body[:200]!r}"
        )
    data = json.loads(body)
    user_id = data["User"]["Id"]
    access_token = data["AccessToken"]
    return user_id, access_token


def main() -> int:
    if os.path.exists(TOKEN_FILE):
        print(f"[bootstrap] {TOKEN_FILE} exists; skipping wizard")
        return 0

    print("[bootstrap] Waiting for Jellyfin /System/Info/Public to respond")
    _wait_for_jellyfin_ready()

    if _wizard_done():
        # Edge case: the wizard was completed by a prior run but
        # the token file got wiped.  We can't recover the token
        # (Jellyfin never re-issues one for an existing session).
        # Best-effort: reset the admin user's password to a fresh
        # value via UpdateUserPassword, then authenticate.  But
        # this requires the OLD password which we don't have.  In
        # practice a wiped token file with a complete wizard means
        # the operator did surgery; tell them to delete the
        # ``data/data/jellyfin.db`` user table or accept manual
        # login from now on.
        print(
            "[bootstrap] wizard already complete but admin-token.json missing; "
            "the auth-proxy will fall through to Jellyfin's login form.  "
            "To recover automatic SSO: stop the container, delete "
            "$OPENHOST_APP_DATA_DIR/data/jellyfin.db (the user DB), restart."
        )
        return 0

    password = _generate_password()
    _drive_wizard(password)
    print("[bootstrap] Wizard complete; minting admin access token")
    user_id, access_token = _authenticate(ADMIN_USERNAME, password)
    server_id = _server_id()

    payload = {
        "server_id": server_id,
        "server_name": SERVER_NAME,
        "user_id": user_id,
        "access_token": access_token,
        "username": ADMIN_USERNAME,
        # We persist the password so the operator can manually log
        # in if they ever need to.  File is mode 0600.
        "password": password,
    }
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    print(f"[bootstrap] Persisted admin token to {TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[bootstrap] uncaught exception: {exc}", file=sys.stderr)
        sys.exit(1)
