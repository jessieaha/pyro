#!/bin/bash
# Link the VU Google Drive to rclone as remote `pyrogdrive` (used by pyro-sync,
# pyro-query and DVC). Run on Snellius; the Google login happens in a browser on
# your laptop. The OAuth token is stored in your own rclone config
# (~/.config/rclone/rclone.conf, mode 600), never in this repository.
#
#   scripts/gdrive_setup.sh                      # link, or repair a remote without token
#   scripts/gdrive_setup.sh --token-file FILE    # same, token JSON read from FILE
#   scripts/gdrive_setup.sh check                # quota + test folder
#   scripts/gdrive_setup.sh status               # linked / no token / missing
#   GDRIVE_SCOPE=drive scripts/gdrive_setup.sh   # full Drive access instead of drive.file
#
# Laptop side (needs rclone installed, https://rclone.org/install/): run the
# `rclone authorize "drive" "<blob>"` command this script prints, log in with the
# VU account, and paste the JSON token it returns (`{"access_token":...}`).
#
# Scope drive.file: rclone only sees files and folders it created itself. A
# `pyro` folder made by hand in the Drive web UI is invisible to it; let rclone
# create the folder (the `check` step does).
#
# Own OAuth client (recommended: rclone's shared client is being retired in 2026,
# and VU may block it with "This app is blocked"): Google Cloud console -> enable
# the Drive API -> OAuth consent screen (Internal) -> Credentials -> OAuth client
# ID, type Desktop app. Export RCLONE_DRIVE_CLIENT_ID / RCLONE_DRIVE_CLIENT_SECRET
# before running this; the printed laptop command then uses the same client.
set -euo pipefail
remote=pyrogdrive
root="${PYRO_GDRIVE_ROOT:-pyro}"
scope="${GDRIVE_SCOPE:-drive.file}"

state() {
    if ! rclone listremotes 2>/dev/null | grep -qx "$remote:"; then
        echo missing
    elif rclone config show "$remote" 2>/dev/null | grep -Eq '^token = \{.*"refresh_token"'; then
        echo linked
    elif [[ -n "${RCLONE_CONFIG_PYROGDRIVE_TOKEN:-}" ]]; then
        echo linked
    else
        echo no_token
    fi
}

check() {
    rclone about "$remote:" || return 1
    rclone mkdir "$remote:$root"
    rclone lsf --max-depth 1 "$remote:$root"
    echo "OK: $remote:$root is reachable"
}

authorize_cmd() {
    if [[ -n "${RCLONE_DRIVE_CLIENT_ID:-}" ]]; then
        # The secret is typed on the laptop, so it is not echoed here.
        printf '%s\n    %s\n' \
            "read -rs CSEC   # paste the client secret, then:" \
            "rclone authorize \"drive\" \"\$(printf '{\"scope\":\"$scope\",\"client_id\":\"$RCLONE_DRIVE_CLIENT_ID\",\"client_secret\":\"%s\"}' \"\$CSEC\" | base64 | tr -d '=\\n')\""
    else
        local blob
        blob=$(printf '{"scope":"%s"}' "$scope" | base64 -w0 | tr -d '=')
        echo "rclone authorize \"drive\" \"$blob\""
    fi
}

valid_token() {
    python3 -c 'import json,sys; t=json.loads(sys.stdin.read()); sys.exit(0 if t.get("refresh_token") else 1)' \
        <<<"$1" 2>/dev/null
}

write_remote() {
    local token=$1 conf
    conf=$(rclone config file | tail -n 1)
    mkdir -p "$(dirname "$conf")"
    rclone config delete "$remote" 2>/dev/null || true
    # printf is a shell builtin: the token never appears on a command line (ps).
    (
        umask 077
        {
            printf '\n[%s]\ntype = drive\nscope = %s\n' "$remote" "$scope"
            [[ -n "${RCLONE_DRIVE_CLIENT_ID:-}" ]] && printf 'client_id = %s\n' "$RCLONE_DRIVE_CLIENT_ID"
            [[ -n "${RCLONE_DRIVE_CLIENT_SECRET:-}" ]] && printf 'client_secret = %s\n' "$RCLONE_DRIVE_CLIENT_SECRET"
            printf 'token = %s\n' "$token"
        } >>"$conf"
    )
    chmod 600 "$conf"
}

token_file=
case "${1:-}" in
    check) check; exit ;;
    status) state; exit ;;
    --token-file) token_file=${2:?--token-file needs a path} ;;
    "") ;;
    *) sed -n '2,25p' "$0"; exit 2 ;;
esac

if [[ -z "$token_file" && "$(state)" == linked ]]; then
    echo "$remote is linked"; check; exit
fi
[[ "$(state)" == no_token ]] && echo "$remote exists but has no OAuth token; linking it now."

if [[ -n "$token_file" ]]; then
    token=$(<"$token_file")
else
    [[ -t 0 ]] || { echo "run interactively, or pass --token-file FILE" >&2; exit 2; }
    if [[ -z "${RCLONE_DRIVE_CLIENT_ID:-}" ]]; then
        echo "NOTE: using rclone's shared OAuth client, which Google is retiring in 2026."
        echo "      If login fails or is blocked, set RCLONE_DRIVE_CLIENT_ID/_SECRET (see header)."
    fi
    cat <<EOF

On your laptop (with rclone installed), run:

    $(authorize_cmd)

log in with your VU Google account, then paste the token it prints
(one line starting with {"access_token":...}). Input is hidden.

EOF
    read -rs -p "token> " token
    echo
fi
token=$(tr -d '\r\n' <<<"$token")
valid_token "$token" || { echo "that is not an rclone Drive token (no refresh_token)" >&2; exit 1; }
write_remote "$token"
unset token
echo "token saved to $(rclone config file | tail -n 1)"
check
