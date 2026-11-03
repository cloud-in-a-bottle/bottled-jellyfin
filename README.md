# bottled-jellyfin

Jellyfin (open-source media server) packaged for Cloud in a Bottle with one-click
owner SSO.

When the Cloud in a Bottle zone owner visits `jellyfin.<zone>` for the first time
in a browser, an HTML shim seeds Jellyfin's `localStorage.jellyfin_credentials`
entry with a browser-specific access token, then redirects to `/web/`.  The SPA
loads with the owner already signed in. Each browser gets a distinct Jellyfin
session, so WebSocket updates and remote-control commands target the right one.

## Topology

```
browser → OpenHost outer Caddy (TLS termination)
       → OpenHost router (subdomain jellyfin.<zone>; verifies owner
                          zone_auth, stamps X-OpenHost-Is-Owner)
       → container :8080  (auth_proxy.py — owner shim sidecar)
       → 127.0.0.1:8096   (Jellyfin .NET server)
```

## Why the localStorage shim, not a Set-Cookie 302

Jellyfin's authentication is API-key based, not cookie-based.  The web SPA
reads `localStorage.jellyfin_credentials`, extracts the AccessToken, and
sets `Authorization: MediaBrowser Token="..."` on every API call.  There
is no server-side session cookie to mint, so the standard Pattern B 302-
with-Set-Cookie pattern (joplin, vscode, bookstack) does not work.

Instead, the auth-proxy detects an owner GET on `/`, `/index.html`, or
similar pre-SPA paths AND no `openhost_jellyfin_seeded` cookie. It authenticates
to Jellyfin over container loopback with a new device ID using the password in
`admin-token.json`, then serves a tiny HTML page that runs JS to:

  1. `localStorage.setItem('jellyfin_credentials', JSON.stringify(...))`
     with the server-minted token, server Id, user Id, and address.
  2. Set `openhost_jellyfin_seeded=<token-marker>` cookie so subsequent
     navigations don't re-shim.
  3. `window.location.replace('/web/')`.

The cookie marker tracks the original bootstrap token, not the per-browser token,
so repeat navigations do not mint additional sessions. If the operator replaces
the bootstrap token, the marker changes and the shim re-fires. The v3 marker
also re-seeds browsers that still hold an older shared token on their next `/` visit.
If an old token file lacks the password or Jellyfin is temporarily unavailable,
the owner can still be seeded with the bootstrap token.
Do not delete `admin-token.json` on an already configured server as a rotation
method: the first-boot wizard does not recreate it after setup is complete.

## Files

  * `cloudinabottle.toml` — manifest. Declares `/_healthz` health check and the
    public paths Jellyfin needs (mobile/desktop client APIs, uppercase and
    lowercase media stream URLs, the WebSocket endpoint).
  * `Dockerfile` — bases on a pinned Jellyfin image, adds
    Python 3 + tini + gosu + curl.
  * `start.sh` — supervises Jellyfin + auth-proxy via bash `wait -n`.
    Spawns `bootstrap_admin.py` in the background to drive the startup
    wizard on first boot.
  * `auth_proxy.py` — owner localStorage-shim sidecar. Mints a distinct
    browser token and renders the shim on owner pre-SPA navigations. It
    streams media responses (including byte ranges and transcoded output)
    without a 256 MiB cap, and tunnels WebSocket upgrades. For non-owner media
    requests it verifies both the Jellyfin token and that the token's user can
    access the requested item; Jellyfin's own stream routes can otherwise serve
    bytes without authentication. It omits query strings from request logs so
    shared-link API keys are never logged by the proxy.
  * `bootstrap_admin.py` — first-boot setup wizard automation.  Drives
    `POST /Startup/Configuration → /Startup/RemoteAccess → /Startup/User
    → /Startup/Complete` then mints an access token via
    `POST /Users/AuthenticateByName`.

## Persistent state

Working state lives under `$OPENHOST_APP_DATA_DIR/` (local NVMe, backed
up — fast and POSIX-strict, so it's safe for Jellyfin's SQLite DBs):

  * `config/` — Jellyfin config XMLs.
  * `data/` — Jellyfin's DBs (jellyfin.db, library.db).  These are
    SQLite and **must** stay on `app_data`; the archive tier's FS can
    corrupt a SQLite WAL.
  * `cache/` — transcode + thumbnail cache.
  * `log/` — Jellyfin logs.
  * `admin-token.json` — bootstrap-generated server id, user id, access
    token and password (mode 0600, never sent to browsers).

Bulk media lives on the **archive tier** under
`$OPENHOST_APP_ARCHIVE_DIR/` (`app_archive = true` in the manifest):

  * `media/` — media library mount point.  Drop video/audio files /
    mounts here.  Backed by JuiceFS — local disk by default, and
    transparently offloaded to **S3** once the operator upgrades the
    zone to S3 storage from the Cloud in a Bottle dashboard.  This keeps large
    video files off the host's local disk. Add the archive paths as Jellyfin
    libraries and run a library scan after copying media files in.

## Caveats

  * **First-boot is slow.**  Jellyfin's initial DB schema migrations and
    plugin scan can take 60+s on a cold container; `/_healthz` returns
    200 from the auth-proxy immediately so Cloud in a Bottle won't flag the app
    as failed during this window.
  * **No transcoding GPU.**  Jellyfin will use software ffmpeg only.
    Hardware acceleration would require GPU passthrough in the Cloud in a Bottle
    runtime config which we don't request.
  * **Mobile/desktop clients log in normally.**  Since the shim only fires
    on browser navigations to `/`, mobile / desktop Jellyfin clients (which
    POST directly to `/Users/AuthenticateByName`) work with their own
    username/password — those credentials are stored in
    `admin-token.json` for the operator to read out.
