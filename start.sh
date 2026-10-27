#!/bin/bash
# Boot Jellyfin + auth-proxy for OpenHost.
#
# Topology:
#
#   browser → OpenHost router (subdomain jellyfin.<zone>; verifies
#                              owner zone_auth, stamps
#                              X-OpenHost-Is-Owner)
#          → container :8080  (auth_proxy.py)
#          → 127.0.0.1:8096   (Jellyfin .NET server)
#
# Auth flow on first owner visit (first browser):
#   1. Owner GETs / on jellyfin.<zone>.  Router stamps
#      X-OpenHost-Is-Owner=true.
#   2. auth_proxy.py sees no openhost_jellyfin_seeded cookie,
#      reads admin-token.json, serves an HTML shim that JS-writes
#      localStorage.jellyfin_credentials and redirects to /web/.
#   3. /web/ loads the SPA which reads localStorage and is now
#      authenticated.
#
# First-boot bootstrap:
#   * Jellyfin's first run drops the server in "startup wizard"
#     state (StartupWizardCompleted=false in
#     $CONFIG_DIR/system.xml).
#   * bootstrap_admin.py drives the wizard:
#       POST /Startup/Configuration
#       POST /Startup/RemoteAccess  (EnableRemoteAccess=true)
#       POST /Startup/User           (admin username + random pw)
#       POST /Startup/Complete
#     then mints an access token via /Users/AuthenticateByName.
#   * Persist {server_id, user_id, access_token, ...} to
#     $OPENHOST_APP_DATA_DIR/admin-token.json (mode 0600) for the
#     auth-proxy to read.

set -euo pipefail

PERSIST="${OPENHOST_APP_DATA_DIR:-/data/app_data/jellyfin}"
TEMP="${OPENHOST_APP_TEMP_DIR:-/tmp}"
# Bulk media lives on the archive tier (JuiceFS; local disk by
# default, S3 once the operator upgrades the zone) so large video
# files don't fill the host's local NVMe.  Fall back to app_data
# if the archive dir isn't granted (e.g. older manifest / dev).
ARCHIVE="${OPENHOST_APP_ARCHIVE_DIR:-$PERSIST}"
ZONE_DOMAIN="${OPENHOST_ZONE_DOMAIN:-localhost}"
APP_NAME="${OPENHOST_APP_NAME:-jellyfin}"
APP_HOST="${APP_NAME}.${ZONE_DOMAIN}"

CONFIG_DIR="$PERSIST/config"
DATA_DIR="$PERSIST/data"
CACHE_DIR="$PERSIST/cache"
LOG_DIR="$PERSIST/log"
# Video/audio files go on the archive tier; everything above
# (config + SQLite DBs + cache + logs) stays on fast local disk.
MEDIA_DIR="$ARCHIVE/media"
TOKEN_FILE="$PERSIST/admin-token.json"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$CACHE_DIR" "$LOG_DIR" "$MEDIA_DIR"

# The upstream jellyfin/jellyfin image runs as root by default;
# Jellyfin doesn't have a dedicated unprivileged user in the image.
# We keep root for simplicity — the only ports bound are 8080 and
# 8096 on loopback; the OpenHost compute_space sandbox confines
# everything else.

# -----------------------------------------------------------------
# Jellyfin config
# -----------------------------------------------------------------

# Jellyfin reads these env vars to override its default paths.
export JELLYFIN_CONFIG_DIR="$CONFIG_DIR"
export JELLYFIN_DATA_DIR="$DATA_DIR"
export JELLYFIN_CACHE_DIR="$CACHE_DIR"
export JELLYFIN_LOG_DIR="$LOG_DIR"
# Jellyfin's web UI is bundled into the image at /jellyfin/jellyfin-web.
export JELLYFIN_WEB_DIR="/jellyfin/jellyfin-web"

echo "[start.sh] Jellyfin config: CONFIG_DIR=$JELLYFIN_CONFIG_DIR DATA_DIR=$JELLYFIN_DATA_DIR MEDIA_DIR=$MEDIA_DIR (archive tier)"

# -----------------------------------------------------------------
# Launch Jellyfin
# -----------------------------------------------------------------
#
# Jellyfin's binary is /jellyfin/jellyfin (a self-contained .NET
# executable).  We pin it to localhost so the only externally-
# reachable port is the auth-proxy on :8080.

echo "[start.sh] Starting Jellyfin on 127.0.0.1:8096"
# Jellyfin's flags: --configdir, --datadir, --cachedir, --logdir,
# --webdir, --service.  Note `--service` is a boolean flag (no
# value) and it disables interactive ttys; we want it because
# we're running under tini.  We do NOT pass --nowebclient — the
# default is to host the web client at /web/.
/jellyfin/jellyfin \
    --configdir "$CONFIG_DIR" \
    --datadir "$DATA_DIR" \
    --cachedir "$CACHE_DIR" \
    --logdir "$LOG_DIR" \
    --webdir "$JELLYFIN_WEB_DIR" \
    --service &
JELLYFIN_PID=$!

# -----------------------------------------------------------------
# Launch auth-proxy IMMEDIATELY so OpenHost's healthcheck on
# /_healthz starts succeeding as soon as the proxy is up.
# -----------------------------------------------------------------

echo "[start.sh] Starting auth-proxy on 0.0.0.0:8080 -> 127.0.0.1:8096"
export AUTH_PROXY_LISTEN_PORT="8080"
export AUTH_PROXY_UPSTREAM_HOST="127.0.0.1"
export AUTH_PROXY_UPSTREAM_PORT="8096"
export AUTH_PROXY_TOKEN_FILE="$TOKEN_FILE"
python3 /opt/openhost-jellyfin/auth_proxy.py &
PROXY_PID=$!

# -----------------------------------------------------------------
# Bootstrap (first boot only): drive Jellyfin's startup wizard +
# mint admin token.  This blocks waiting for Jellyfin to bind its
# port; it's safe to run in the background since we don't care
# about the result of the wizard for proxy supervision.
# -----------------------------------------------------------------

(
    JELLYFIN_PORT=8096 \
    TOKEN_FILE="$TOKEN_FILE" \
    OPENHOST_ZONE_DOMAIN="$ZONE_DOMAIN" \
    JELLYFIN_SERVER_NAME="OpenHost ($APP_HOST)" \
    python3 /opt/openhost-jellyfin/bootstrap_admin.py 2>&1 \
    | sed 's/^/[bootstrap] /'
) &

# -----------------------------------------------------------------
# Supervision
# -----------------------------------------------------------------

trap 'kill -TERM "$JELLYFIN_PID" "$PROXY_PID" 2>/dev/null; wait' TERM INT

set +e
wait -n "$JELLYFIN_PID" "$PROXY_PID"
EXIT_CODE=$?
set -e

echo "[start.sh] Child exited (code=$EXIT_CODE); shutting down"
kill -TERM "$JELLYFIN_PID" "$PROXY_PID" 2>/dev/null || true
wait || true
exit "$EXIT_CODE"
