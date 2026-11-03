#!/bin/bash
# Link the VU Google Drive to rclone as remote `pyrogdrive` (used by pyro-sync,
# pyro-query and DVC). Run interactively on Snellius; the Google login happens in
# a browser on your laptop. The OAuth token is stored in your own
# ~/.config/rclone/rclone.conf (mode 600), never in this repository.
#
#   scripts/gdrive_setup.sh            # link (scope drive.file: rclone only sees
#                                      # files it created itself)
#   GDRIVE_SCOPE=drive scripts/gdrive_setup.sh   # full Drive access instead
#   scripts/gdrive_setup.sh check      # quota + test folder
#
# Laptop side (needs rclone installed): when prompted, run the printed
#   rclone authorize "drive" "<blob>"
# log in with the VU account, and paste the returned token back here.
# If VU blocks rclone's shared OAuth app ("This app is blocked"), create your own
# OAuth client (Google Cloud console, Desktop app) and export
# RCLONE_DRIVE_CLIENT_ID / RCLONE_DRIVE_CLIENT_SECRET before running this.
set -euo pipefail
remote=pyrogdrive
root="${PYRO_GDRIVE_ROOT:-pyro}"

check() {
    rclone about "$remote:" || return 1
    rclone mkdir "$remote:$root"
    rclone lsf --max-depth 1 "$remote:$root"
    echo "OK: $remote:$root is reachable"
}

if [[ "${1:-}" == "check" ]]; then check; exit; fi
if rclone listremotes | grep -qx "$remote:"; then
    echo "$remote already configured"; check; exit
fi
[[ -t 0 ]] || { echo "run this interactively (needs the pasted token)" >&2; exit 2; }

args=(scope="${GDRIVE_SCOPE:-drive.file}" config_is_local=false)
[[ -n "${RCLONE_DRIVE_CLIENT_ID:-}" ]] && args+=(client_id="$RCLONE_DRIVE_CLIENT_ID")
[[ -n "${RCLONE_DRIVE_CLIENT_SECRET:-}" ]] && args+=(client_secret="$RCLONE_DRIVE_CLIENT_SECRET")
rclone config create "$remote" drive "${args[@]}"
chmod 600 "$(rclone config file | tail -n 1)"
check
