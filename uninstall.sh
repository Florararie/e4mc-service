#!/usr/bin/env bash
# Remove the e4mc tunnel and the Pages link watcher installed by install.sh.
#
# Default: stop and disable both services, remove their unit files and the
# install directory. The env file and the state directory are kept so a
# reinstall keeps its token and the last known link; --purge removes those too.
#
# The GitHub repository and its Pages site are never touched.

set -euo pipefail

PREFIX=/opt/e4mc
STATE_DIR=/var/lib/e4mc
SERVICE_USER=e4mc
ENV_DIR=/etc/e4mc
ENV_FILE=$ENV_DIR/e4mc.env
UNIT_DIR=/etc/systemd/system
UNITS=(e4mc-tunnel.service e4mc-watcher.service)

PURGE=0
KEEP_CODE=0
DRY=0

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }

# Print the command in a dry run, execute it otherwise.
act() {
    if [[ $DRY -eq 1 ]]; then
        printf '    would: %s\n' "$*"
    else
        "$@"
    fi
}

usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh [options]

Options:
  --prefix DIR      install directory to remove   (default /opt/e4mc)
  --state-dir DIR   shared state directory        (default /var/lib/e4mc)
  --user NAME       service account               (default e4mc)
  --keep-code       remove the services but not the installed code
  --purge           also remove the env file, the state directory and the
                    service account if it is a system account
  --dry-run         print what would be removed and change nothing
  -h, --help        show this help

Without --purge the GitHub token in /etc/e4mc/e4mc.env and the last link in
the state directory are kept, so reinstalling resumes where it left off.
EOF
}

while (($#)); do
    case $1 in
        --prefix) PREFIX=${2:?--prefix needs a value}; shift 2 ;;
        --state-dir) STATE_DIR=${2:?--state-dir needs a value}; shift 2 ;;
        --user) SERVICE_USER=${2:?--user needs a value}; shift 2 ;;
        --keep-code) KEEP_CODE=1; shift ;;
        --purge) PURGE=1; shift ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

# Never operate on a bare system directory, even if someone passes one.
valid_target() {
    [[ $1 =~ ^/[^/]+/.+$ ]] || return 1
    case $1 in
        /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib|/var/log) return 1 ;;
    esac
    return 0
}

valid_target "$PREFIX" || die "refusing to remove '$PREFIX'; pass the real --prefix"
valid_target "$STATE_DIR" || die "refusing to remove '$STATE_DIR'; pass the real --state-dir"

if [[ $DRY -eq 0 && $EUID -ne 0 ]]; then
    die "run as root, for example: sudo $0"
fi

say "stopping and disabling the services"
if command -v systemctl >/dev/null; then
    if [[ $DRY -eq 1 ]]; then
        act systemctl disable --now "${UNITS[@]}"
    else
        systemctl disable --now "${UNITS[@]}" 2>/dev/null || true
    fi
fi

say "removing unit files from $UNIT_DIR"
for unit in "${UNITS[@]}"; do
    if [[ -e "$UNIT_DIR/$unit" ]]; then
        act rm -f "$UNIT_DIR/$unit"
    fi
done
if command -v systemctl >/dev/null; then
    if [[ $DRY -eq 1 ]]; then
        act systemctl daemon-reload
    else
        systemctl daemon-reload 2>/dev/null || true
    fi
fi

if [[ $KEEP_CODE -eq 1 ]]; then
    say "keeping the installed code at $PREFIX (--keep-code)"
else
    say "removing the installed code and virtualenv at $PREFIX"
    act rm -rf "$PREFIX"
fi

if [[ $PURGE -eq 1 ]]; then
    say "removing configuration $ENV_FILE"
    act rm -f "$ENV_FILE"
    if [[ -d $ENV_DIR ]]; then
        if [[ $DRY -eq 1 ]]; then
            act rmdir "$ENV_DIR"
        else
            rmdir "$ENV_DIR" 2>/dev/null || true
        fi
    fi

    say "removing state $STATE_DIR"
    act rm -rf "$STATE_DIR"

    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        uid=$(id -u "$SERVICE_USER")
        shell=$(awk -F: -v name="$SERVICE_USER" '$1 == name { print $7 }' /etc/passwd)
        if ((uid < 1000)) && [[ $shell == */nologin || $shell == */false ]]; then
            say "removing service account $SERVICE_USER"
            act userdel "$SERVICE_USER"
        else
            printf 'warning: keeping account %s (uid %s, shell %s); delete it yourself if you meant to\n' \
                "$SERVICE_USER" "$uid" "$shell"
        fi
    fi
fi

if [[ $DRY -eq 1 ]]; then
    printf '\nDry run only; nothing was changed.\n'
elif [[ $PURGE -eq 0 ]]; then
    cat <<EOF

Removed the services and $PREFIX. Kept for a reinstall:
  $ENV_FILE    (holds the token; --purge removes it)
  $STATE_DIR   (last link; --purge removes it)

The GitHub repository and its Pages file are untouched.
EOF
else
    cat <<EOF

Removed the services, $PREFIX, the configuration and the state (and the service
account when it was a system account).
The GitHub repository and its Pages file are untouched: index.html is still
published there, so delete that file (or the repository) if you want the old
redirect gone.
EOF
fi
