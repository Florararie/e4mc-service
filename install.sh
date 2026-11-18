#!/usr/bin/env bash
# Install the e4mc tunnel and, optionally, the link watcher on one machine.
#
# Both services run as the same unprivileged account and share STATE_DIR, so the
# watcher can read the tunnel's link from local disk. Nothing is hard-coded to a
# particular repository or host: every path, account, repository and webhook is
# an option.
#
# Publishing is optional and granular. Pass --repo/--token for a GitHub Pages
# page, --webhook for a Discord announcement, both for both, or neither to
# install the tunnel on its own (the watcher is then not installed at all).

set -euo pipefail

PREFIX=/opt/e4mc
STATE_DIR=/var/lib/e4mc
SERVICE_USER=e4mc
MC_HOST=127.0.0.1
MC_PORT=25565
PAGES_REPO=
PAGES_BRANCH=main
PAGES_PATH=index.html
GITHUB_TOKEN=${GITHUB_TOKEN:-}
TOKEN_FILE=
WEBHOOK_URL=
START=1
FORCE=0

SOURCE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_DIR=/etc/e4mc
ENV_FILE=$ENV_DIR/e4mc.env

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*"; }

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [options]

Everything but the tunnel itself is optional. Give --repo and --token to publish
a GitHub Pages page, --webhook to post new addresses to Discord, both for both,
or neither to install the tunnel alone and publish nothing anywhere.

Page options (both required together):
  --repo OWNER/NAME     repository whose Pages site gets the page
  --token-file PATH     file holding a token with Contents: Read and write
  --token VALUE         the token itself (prefer --token-file)

Webhook option:
  --webhook URL         Discord webhook posted to whenever the address changes

Other options:
  --prefix DIR          install directory            (default /opt/e4mc)
  --state-dir DIR       shared state directory       (default /var/lib/e4mc)
  --user NAME           service account              (default e4mc, created if absent)
  --mc-host HOST        Minecraft server address     (default 127.0.0.1)
  --mc-port PORT        Minecraft server port        (default 25565)
  --branch NAME         Pages branch                 (default main)
  --pages-path PATH     file to commit               (default index.html)
  --force               rewrite an existing /etc/e4mc/e4mc.env
  --no-start            enable the units but do not start them
  -h, --help            show this help

Re-running to update the code keeps the existing env file unless --force is given.
To remove the services again, run ./uninstall.sh (see its --help).
EOF
}

while (($#)); do
    case $1 in
        --prefix) PREFIX=${2:?--prefix needs a value}; shift 2 ;;
        --state-dir) STATE_DIR=${2:?--state-dir needs a value}; shift 2 ;;
        --user) SERVICE_USER=${2:?--user needs a value}; shift 2 ;;
        --mc-host) MC_HOST=${2:?--mc-host needs a value}; shift 2 ;;
        --mc-port) MC_PORT=${2:?--mc-port needs a value}; shift 2 ;;
        --repo) PAGES_REPO=${2:?--repo needs a value}; shift 2 ;;
        --branch) PAGES_BRANCH=${2:?--branch needs a value}; shift 2 ;;
        --pages-path) PAGES_PATH=${2:?--pages-path needs a value}; shift 2 ;;
        --token) GITHUB_TOKEN=${2:?--token needs a value}; shift 2 ;;
        --token-file) TOKEN_FILE=${2:?--token-file needs a value}; shift 2 ;;
        --webhook) WEBHOOK_URL=${2:?--webhook needs a value}; shift 2 ;;
        --force) FORCE=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run as root, for example: sudo $0 --webhook 'https://discord.com/api/webhooks/...'"
command -v systemctl >/dev/null || die "systemd is required"
command -v useradd >/dev/null || die "useradd is required"

PYTHON=${PYTHON:-python3}
command -v "$PYTHON" >/dev/null || die "python3 is required"
"$PYTHON" - <<'PY' || die "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

if [[ -n $TOKEN_FILE ]]; then
    [[ -r $TOKEN_FILE ]] || die "cannot read token file: $TOKEN_FILE"
    GITHUB_TOKEN=$(head -n1 "$TOKEN_FILE" | tr -d '\r\n')
fi

WRITE_ENV=1
if [[ -f $ENV_FILE && $FORCE -ne 1 ]]; then
    WRITE_ENV=0
    log "keeping existing $ENV_FILE (use --force to rewrite it)"
fi

if [[ $WRITE_ENV -eq 1 ]]; then
    if [[ -n $PAGES_REPO || -n $GITHUB_TOKEN ]]; then
        [[ $PAGES_REPO =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] ||
            die "--repo must look like owner/name (got '$PAGES_REPO')"
        [[ -n $GITHUB_TOKEN ]] ||
            die "a token is required with --repo: --token-file PATH, --token VALUE, or GITHUB_TOKEN"
        if [[ $GITHUB_TOKEN =~ [[:space:]\"\'] ]]; then
            die "the token must not contain whitespace or quotes"
        fi
    fi
    if [[ -n $WEBHOOK_URL ]]; then
        [[ $WEBHOOK_URL == https://* ]] ||
            die "--webhook must be the https:// link Discord gave you"
        if [[ $WEBHOOK_URL =~ [[:space:]\"\'] ]]; then
            die "the webhook link must not contain whitespace or quotes"
        fi
    fi
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "creating system account $SERVICE_USER"
    NOLOGIN=$(command -v nologin || echo /bin/false)
    useradd --system --home-dir "$PREFIX" --shell "$NOLOGIN" --user-group "$SERVICE_USER"
fi
SERVICE_GROUP=$(id -gn "$SERVICE_USER")

log "installing code to $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/e4mc_tunnel" "$PREFIX/e4mc_watcher"
cp -a "$SOURCE/e4mc_tunnel" "$SOURCE/e4mc_watcher" "$PREFIX/"
install -m 0644 "$SOURCE/requirements.txt" "$SOURCE/README.md" "$PREFIX/"
install -m 0755 "$SOURCE/uninstall.sh" "$PREFIX/"
chown -R root:root "$PREFIX"

log "creating the virtualenv"
"$PYTHON" -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$PREFIX/requirements.txt"

log "preparing $STATE_DIR (shared by both services)"
install -d -m 0750 "$STATE_DIR"
chown "$SERVICE_USER:$SERVICE_GROUP" "$STATE_DIR"
chmod 0750 "$STATE_DIR"

install -d -m 0755 "$ENV_DIR"
if [[ $WRITE_ENV -eq 1 ]]; then
    log "writing $ENV_FILE"
    umask 077
    cat >"$ENV_FILE" <<EOF
# Read by e4mc-tunnel.service and e4mc-watcher.service. Keep this private:
# it holds the GitHub token and the Discord webhook. The tunnel unit unsets
# both, so only the watcher ever has them in its environment. STATE_DIR is
# fixed by --state-dir in both units; blank publisher settings are skipped.
MC_HOST=$MC_HOST
MC_PORT=$MC_PORT
BROKER_URL=https://broker.e4mc.link/getBestRelay
DOMAIN_SUFFIX=.e4mc.link
KEEPALIVE_INTERVAL=15
IDLE_TIMEOUT=30
RECONNECT_MIN=1
RECONNECT_MAX=30
HEARTBEAT_INTERVAL=20
LOG_LEVEL=INFO
GITHUB_TOKEN=$GITHUB_TOKEN
PAGES_REPO=$PAGES_REPO
PAGES_BRANCH=$PAGES_BRANCH
PAGES_PATH=$PAGES_PATH
PAGES_COMMIT_AUTHOR="GitHub <noreply@github.com>"
PAGE_AUTHOR=Florararie
DISCORD_WEBHOOK_URL=$WEBHOOK_URL
POLL_INTERVAL=5
STATE_MAX_AGE=90
EOF
    chmod 0600 "$ENV_FILE"
fi

# The watcher is only worth installing when it has somewhere to publish. When
# re-running against an existing env file, read the answer from that file.
WATCHER=0
if [[ -n $PAGES_REPO || -n $GITHUB_TOKEN || -n $WEBHOOK_URL ]]; then
    WATCHER=1
elif [[ $WRITE_ENV -eq 0 ]] &&
    grep -Eq '^(GITHUB_TOKEN|PAGES_REPO|DISCORD_WEBHOOK_URL)=.+' "$ENV_FILE"; then
    WATCHER=1
fi

UNITS=(e4mc-tunnel.service)
if [[ $WATCHER -eq 1 ]]; then
    UNITS+=(e4mc-watcher.service)
else
    log "no page or webhook configured: installing the tunnel on its own"
fi

log "installing units"
for unit in "${UNITS[@]}"; do
    sed -e "s|@PREFIX@|$PREFIX|g" \
        -e "s|@STATE_DIR@|$STATE_DIR|g" \
        -e "s|@USER@|$SERVICE_USER|g" \
        -e "s|@GROUP@|$SERVICE_GROUP|g" \
        "$SOURCE/deploy/$unit.service" >"/etc/systemd/system/$unit.service"
    chmod 0644 "/etc/systemd/system/$unit.service"
done
if [[ $WATCHER -eq 0 && -e /etc/systemd/system/e4mc-watcher.service ]]; then
    log "disabling the watcher unit from an earlier install"
    systemctl disable --now e4mc-watcher.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/e4mc-watcher.service
fi

systemctl daemon-reload
systemctl enable "${UNITS[@]}" >/dev/null
if [[ $START -eq 1 ]]; then
    systemctl restart "${UNITS[@]}"
fi

SERVICE_LIST=$(printf '%s, ' "${UNITS[@]}")
SERVICE_LIST=${SERVICE_LIST%, }

printf '\nInstalled.\n\n'
printf '  code      %s\n' "$PREFIX"
printf '  config    %s   (root only, mode 0600)\n' "$ENV_FILE"
printf '  state     %s/state.json and link.txt\n' "$STATE_DIR"
printf '  services  %s\n' "$SERVICE_LIST"
if [[ -n $PAGES_REPO ]]; then
    printf '  page      %s (%s on branch %s)\n' "$PAGES_REPO" "$PAGES_PATH" "$PAGES_BRANCH"
else
    printf '  page      not configured\n'
fi
if [[ -n $WEBHOOK_URL ]]; then
    printf '  webhook   configured\n'
else
    printf '  webhook   not configured\n'
fi

printf '\nNext:\n'
if [[ -n $PAGES_REPO ]]; then
    printf '  1. Enable Pages once at https://github.com/%s/settings/pages\n' "$PAGES_REPO"
    printf '     (Deploy from a branch: %s / root).\n' "$PAGES_BRANCH"
fi
printf '  * Watch the first assignment:  journalctl -u e4mc-tunnel -f\n'
if [[ $WATCHER -eq 1 ]]; then
    printf '  * Watch the destination:       journalctl -u e4mc-watcher -f\n'
fi
cat <<'EOF'

Players connect with the address the page shows, not the page URL: Minecraft
needs a bare hostname, so https:// and /path cannot be typed into it.

Edit /etc/e4mc/e4mc.env and run 'systemctl restart e4mc-tunnel e4mc-watcher' to
reconfigure. To add or drop a destination later, edit those keys there and
re-run this installer without publisher flags: it reads the env file and
installs or removes the watcher to match. --force rewrites the whole file from
the options given here.
Remove everything again with 'sudo /opt/e4mc/uninstall.sh' (add --purge to also
remove the config, the state and the service account).
EOF
