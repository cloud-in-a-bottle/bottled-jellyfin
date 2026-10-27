import json
import re
import time
from typing import Any

import requests
from openhost_test_harness import OpenhostStack

# The shim writes this JSON literal into localStorage.jellyfin_credentials (see auth_proxy._build_shim_html).
_CREDS_RE = re.compile(r"var creds = (\{.*\});")


def _context(stack: OpenhostStack, response: requests.Response | None) -> str:
    summary = "no response" if response is None else f"{response.status_code}: {response.text[:500]}"
    return f"{summary}\n\napp logs:\n{stack.app_logs()[-5000:]}"


def _owner_creds(stack: OpenhostStack) -> dict[str, Any]:
    """Fetch the owner's first navigation to "/" and return the credentials the SSO shim seeds."""
    # Jellyfin's first boot (schema migration, then bootstrap_admin.py driving the wizard) can take a few minutes.
    deadline = time.monotonic() + 300
    response = None
    while time.monotonic() < deadline:
        try:
            response = stack.owner_session.get(f"{stack.url}/", headers={"Accept": "text/html"}, timeout=30)
        except requests.ConnectionError:
            response = None
        else:
            match = _CREDS_RE.search(response.text)
            if response.status_code == 200 and match:
                return json.loads(match.group(1))  # type: ignore[no-any-return]
        time.sleep(3)
    raise AssertionError(f"owner GET / never served the SSO shim: {_context(stack, response)}")


def _wait_for_setup(stack: OpenhostStack) -> None:
    """Wait until bootstrap_admin.py has completed Jellyfin's startup wizard."""
    deadline = time.monotonic() + 300
    response = None
    while time.monotonic() < deadline:
        try:
            response = stack.owner_session.get(f"{stack.url}/System/Info/Public", timeout=30)
        except requests.ConnectionError:
            response = None
        else:
            if response.status_code == 200 and response.json().get("StartupWizardCompleted"):
                return
        time.sleep(3)
    raise AssertionError(f"the startup wizard never completed: {_context(stack, response)}")


def test_owner_sso(stack: OpenhostStack) -> None:
    # The owner lands in Jellyfin signed in as the bootstrapped admin: the shim's token is a working admin token.
    server = _owner_creds(stack)["Servers"][0]
    response = stack.owner_session.get(
        f"{stack.url}/Users/Me",
        headers={"X-Emby-Token": server["AccessToken"], "Accept": "application/json"},
        timeout=30,
    )
    assert response.status_code == 200, f"/Users/Me with the shim's token: {_context(stack, response)}"
    me = response.json()
    assert me["Id"] == server["UserId"], me
    assert me["Name"] == "owner", me
    assert me["Policy"]["IsAdministrator"], me


def test_anonymous_access(stack: OpenhostStack) -> None:
    _wait_for_setup(stack)
    # Private paths (the SSO shim on "/", the authenticated API) are refused before reaching the app.
    for path in ("/", "/Users/Me"):
        response = requests.get(
            f"{stack.url}{path}", headers={"Accept": "text/html"}, allow_redirects=False, timeout=30
        )
        assert response.status_code >= 300, f"anonymous GET {path}: {_context(stack, response)}"
        assert "jellyfin_credentials" not in response.text, f"anonymous GET {path} got the SSO shim"
    # The public paths in cloudinabottle.toml reach Jellyfin, so other clients can discover the server and log in.
    response = requests.get(f"{stack.url}/System/Info/Public", timeout=30)
    assert response.status_code == 200, f"anonymous GET /System/Info/Public: {_context(stack, response)}"
    assert response.json()["Id"], response.text
    response = requests.get(f"{stack.url}/web/", headers={"Accept": "text/html"}, timeout=30)
    assert response.status_code == 200, f"anonymous GET /web/: {_context(stack, response)}"


def test_state_in_app_data(stack: OpenhostStack) -> None:
    # start.sh points Jellyfin's config and databases at the app data dir; upstream's default (/config) isn't kept.
    _wait_for_setup(stack)  # the wizard has written the config and the user database by now
    assert (stack.data_dir / "config" / "system.xml").is_file(), sorted(p.name for p in stack.data_dir.iterdir())
    databases = sorted(p.name for p in (stack.data_dir / "data").glob("*.db"))
    assert databases, f"no Jellyfin database under {stack.data_dir / 'data'}"
