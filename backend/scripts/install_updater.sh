#!/usr/bin/env bash
# Install the Launchpad online updater (docs/design online-update §2.4). Run once, as root:
#
#   sudo backend/scripts/install_updater.sh                 # defaults from this checkout
#   sudo backend/scripts/install_updater.sh --user ubuntu --services "launchpad-backend"
#
# Writes
#   /usr/local/lib/launchpad/apply_update.sh   (a COPY, root-owned: an update never
#                                              replaces the script that applies it)
#   /etc/launchpad-updater.env                 (repo, data dir, owner, units, health URL)
#   /etc/systemd/system/launchpad-updater.service  (Type=oneshot, runs the copy)
#   /etc/systemd/system/launchpad-updater.path     (PathExists=$DATA_DIR/updates/apply.request)
# then enables the .path unit. Re-run it after pulling a new apply_update.sh on purpose.
#
# Options: --repo DIR (default: this checkout), --data DIR (default $REPO/data),
#   --user NAME (default: the checkout's owner), --services "UNITS" (default: whichever
#   of launchpad-backend / launchpad-frontend exist), --health-url URL,
#   --ledger PATH (default $DATA/launchpad.db).
# LAUNCHPAD_UPDATER_ROOT=<dir> installs under <dir> instead of / (tests; no root needed);
# LAUNCHPAD_SYSTEMCTL overrides systemctl.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
DATA=""
OWNER=""
SERVICES=""
HEALTH_URL=http://127.0.0.1:8000/api/auth/status
LEDGER=""
ROOT=${LAUNCHPAD_UPDATER_ROOT:-}
SYSTEMCTL=${LAUNCHPAD_SYSTEMCTL:-systemctl}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO=$(cd "$2" && pwd); shift 2 ;;
    --data) DATA=$2; shift 2 ;;
    --user) OWNER=$2; shift 2 ;;
    --services) SERVICES=$2; shift 2 ;;
    --health-url) HEALTH_URL=$2; shift 2 ;;
    --ledger) LEDGER=$2; shift 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

if [ -z "$ROOT" ] && [ "$(id -u)" != 0 ]; then
  echo "install_updater.sh must run as root (sudo)" >&2
  exit 1
fi
[ -d "$REPO/.git" ] || [ -f "$REPO/.git" ] || { echo "$REPO is not a git checkout" >&2; exit 1; }
DATA=${DATA:-$REPO/data}
LEDGER=${LEDGER:-$DATA/launchpad.db}
OWNER=${OWNER:-$(stat -c %U "$REPO" 2>/dev/null || stat -f %Su "$REPO")}
if [ -z "$SERVICES" ]; then
  for unit in launchpad-backend launchpad-frontend; do
    if "$SYSTEMCTL" cat "$unit" >/dev/null 2>&1; then SERVICES="${SERVICES:+$SERVICES }$unit"; fi
  done
  SERVICES=${SERVICES:-launchpad-backend}
fi
PATH_ENV=$(dirname "$(command -v uv 2>/dev/null || echo /usr/local/bin/uv)"):/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin

LIB=$ROOT/usr/local/lib/launchpad
UNITS=$ROOT/etc/systemd/system
ENV_FILE=$ROOT/etc/launchpad-updater.env
mkdir -p "$LIB" "$UNITS"

install -m 0755 "$HERE/apply_update.sh" "$LIB/apply_update.sh"
if [ -z "$ROOT" ]; then chown root:root "$LIB/apply_update.sh"; fi

cat >"$ENV_FILE" <<ENV
LAUNCHPAD_REPO_DIR=$REPO
LAUNCHPAD_DATA_DIR=$DATA
LAUNCHPAD_OWNER=$OWNER
LAUNCHPAD_SERVICES=$SERVICES
LAUNCHPAD_HEALTH_URL=$HEALTH_URL
LAUNCHPAD_LEDGER_DB=$LEDGER
PATH=$PATH_ENV
ENV
chmod 0644 "$ENV_FILE"

cat >"$UNITS/launchpad-updater.service" <<UNIT
[Unit]
Description=Launchpad online updater (applies $DATA/updates/apply.request)
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/launchpad-updater.env
ExecStart=/usr/local/lib/launchpad/apply_update.sh
TimeoutStartSec=1800
UNIT

cat >"$UNITS/launchpad-updater.path" <<UNIT
[Unit]
Description=Watch for a Launchpad online update request

[Path]
PathExists=$DATA/updates/apply.request
Unit=launchpad-updater.service

[Install]
WantedBy=multi-user.target
UNIT

install -d -m 0755 "$DATA/updates"
if [ -z "$ROOT" ] || [ "$(id -u)" = 0 ]; then chown "$OWNER" "$DATA/updates" 2>/dev/null || true; fi

"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now launchpad-updater.path
echo "launchpad-updater installed: repo=$REPO owner=$OWNER units=\"$SERVICES\""
