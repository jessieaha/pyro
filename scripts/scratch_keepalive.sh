#!/bin/bash
# Start (or show) the scratch keep-alive chain.
#   scripts/scratch_keepalive.sh start [DIR ...]   copy the job script to a stable
#                                                  location and submit it now
#   scripts/scratch_keepalive.sh status            list pending/running links
#   scripts/scratch_keepalive.sh stop              cancel the chain
# Env: PYRO_SLURM_ACCOUNT (required for start), PYRO_KEEPALIVE_DAYS (default 12).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name=pyro-scratch-keepalive
state="$HOME/.local/state/pyro"
installed="$state/scratch_keepalive.sbatch"

case "${1:-status}" in
start)
    shift
    : "${PYRO_SLURM_ACCOUNT:?set PYRO_SLURM_ACCOUNT}"
    days="${PYRO_KEEPALIVE_DAYS:-12}"
    if (( days >= 14 )); then echo "PYRO_KEEPALIVE_DAYS must be < 14" >&2; exit 2; fi
    if [[ -n "$(squeue -h -u "$USER" -n "$name" -o %i)" ]]; then
        echo "a $name chain already exists:"; squeue -u "$USER" -n "$name"; exit 0
    fi
    mkdir -p "$state"
    install -m 0755 "$here/slurm/scratch_keepalive.sbatch" "$installed"
    dirs="${*:-/scratch-shared/$USER}"
    PYRO_KEEPALIVE_SCRIPT="$installed" PYRO_KEEPALIVE_DIRS="$dirs" PYRO_KEEPALIVE_DAYS="$days" \
        sbatch --export=ALL --account="$PYRO_SLURM_ACCOUNT" \
        --output="$state/keepalive-%j.out" "$installed"
    ;;
status)
    squeue -u "$USER" -n "$name" -o "%.10i %.9T %.20S %.10M %R"
    last=$(ls -t "$state"/keepalive-*.out 2>/dev/null | head -1 || true)
    [[ -n "$last" ]] && tail -n 3 "$last"
    true
    ;;
stop)
    scancel -u "$USER" -n "$name"
    ;;
*)
    echo "usage: $0 start [DIR ...] | status | stop" >&2; exit 2
    ;;
esac
