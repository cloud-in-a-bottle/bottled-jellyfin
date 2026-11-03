"""Owner SSO through a real browser: the shim's credentials must actually get jellyfin-web past its loading screen.

test_owner_auth.py checks the shim's token works against the API, which is not enough: jellyfin-web picks the server
address from the seeded entry by its LastConnectionMode, and a mode with no matching address left the web client stuck
on its loading screen ("Must supply a serverAddress") while every API check still passed.
"""

import json
import re
import time
from typing import Any

from openhost_test_harness import OpenhostStack
from playwright.sync_api import Page, expect

# jellyfin-web lands here once it has connected to the server with the seeded credentials.
_HOME_URL = re.compile(r"/web/#/home")
# Jellyfin's first boot plus bootstrap_admin.py can take minutes; the shim is only served once the token exists.
_BOOT_TIMEOUT_SECONDS = 300
_HOME_TIMEOUT_MS = 60_000


def _wait_for_admin_token(stack: OpenhostStack) -> dict[str, Any]:
    token_file = stack.data_dir / "admin-token.json"
    deadline = time.monotonic() + _BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            return json.loads(token_file.read_text())  # type: ignore[no-any-return]
        except (FileNotFoundError, ValueError):
            time.sleep(3)
    raise AssertionError(f"bootstrap never wrote {token_file}:\n{stack.app_logs()[-5000:]}")


def _seeded_server(page: Page) -> dict[str, Any]:
    raw = page.evaluate("localStorage.getItem('jellyfin_credentials')")
    assert raw, "the shim did not seed jellyfin_credentials"
    return json.loads(raw)["Servers"][0]  # type: ignore[no-any-return]


def _origin(page: Page) -> str:
    return page.evaluate("window.location.origin")  # type: ignore[no-any-return]


def _expect_home(page: Page, errors: list[str]) -> None:
    expect(page).to_have_url(_HOME_URL, timeout=_HOME_TIMEOUT_MS)
    assert not errors, f"jellyfin-web threw: {errors}"


def test_owner_lands_on_home(stack: OpenhostStack, page: Page) -> None:
    _wait_for_admin_token(stack)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    stack.playwright_login(page)

    page.goto(f"{stack.url}/")
    _expect_home(page, errors)

    server = _seeded_server(page)
    assert server["LastConnectionMode"] == 2, server  # ConnectionMode.Manual
    # Addresses follow how the browser reached the app (plain http in the harness, https on public zones).
    assert server["ManualAddress"] == _origin(page), server

    # A later visit to / is not shimmed again: the seeded cookie survives (it is only Secure over https). The shim
    # answers / itself with a 200 and navigates away (so its body can't be read afterwards); once seeded, / is
    # proxied and Jellyfin redirects it to /web/.
    root_statuses: list[int] = []
    page.on(
        "response",
        lambda r: (
            root_statuses.append(r.status)
            if r.request.resource_type == "document" and r.url.rstrip("/") == stack.url
            else None
        ),
    )
    page.goto(f"{stack.url}/")
    _expect_home(page, errors)
    assert root_statuses and all(300 <= s < 400 for s in root_statuses), (
        f"/ was not proxied to Jellyfin on an already-seeded browser (statuses {root_statuses}): the shim ran again"
    )


def test_reseeds_browser_seeded_by_old_shim(stack: OpenhostStack, page: Page) -> None:
    """A browser the v1 shim seeded (mode Local, no LocalAddress, unversioned cookie) is fixed on its next visit."""
    creds = _wait_for_admin_token(stack)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    stack.playwright_login(page)

    # Reproduce exactly what the v1 shim left behind, on the app's origin.
    page.goto(f"{stack.url}/System/Info/Public")
    origin = _origin(page)
    broken = {
        "Servers": [
            {
                "Id": creds["server_id"],
                "Name": "Jellyfin",
                "AccessToken": creds["access_token"],
                "UserId": creds["user_id"],
                "ManualAddress": origin,
                "RemoteAddress": origin,
                "LastConnectionMode": 0,
                "DateLastAccessed": 0,
                "Type": "Server",
            }
        ]
    }
    page.evaluate("c => localStorage.setItem('jellyfin_credentials', c)", json.dumps(broken))
    page.context.add_cookies(
        [{"name": "openhost_jellyfin_seeded", "value": creds["access_token"][-12:], "url": origin}]
    )

    # Sanity check: those credentials are the bug.
    page.goto(f"{stack.url}/web/")
    page.wait_for_timeout(5_000)
    assert any("serverAddress" in e for e in errors), f"the v1 credentials did not reproduce the hang: {errors}"
    errors.clear()

    page.goto(f"{stack.url}/")
    _expect_home(page, errors)
    assert _seeded_server(page)["LastConnectionMode"] == 2
