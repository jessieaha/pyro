#!/bin/bash
# Refresh modification times under scratch-shared so the 14-day purge does not
# remove working data, then resubmit itself to run again after N days.
# Install/start with scripts/scratch_keepalive.sh (keeps a single chain).
#SBATCH --job-name=pyro-scratch-keepalive
#SBATCH --partition=staging
#SBATCH --time=00:30:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G

set -uo pipefail
script="${PYRO_KEEPALIVE_SCRIPT:?absolute path of this script}"
days="${PYRO_KEEPALIVE_DAYS:-12}"
read -r -a dirs <<<"${PYRO_KEEPALIVE_DIRS:-/scratch-shared/$USER}"

# Resubmit first so a failure below cannot break the chain; skip if another
# link of the chain is already pending.
others=$(squeue -h -u "$USER" -n "$SLURM_JOB_NAME" -t PENDING -o %i | grep -vx "$SLURM_JOB_ID")
if [[ -z "$others" ]]; then
    sbatch --parsable --begin="now+${days}days" --export=ALL \
        ${SLURM_JOB_ACCOUNT:+--account="$SLURM_JOB_ACCOUNT"} \
        --output="${PYRO_KEEPALIVE_LOG:-$HOME/.local/state/pyro/keepalive-%j.out}" \
        "$script" || echo "WARNING: resubmission failed" >&2
fi

for d in "${dirs[@]}"; do
    [[ -d "$d" ]] || { echo "skip missing $d"; continue; }
    n=$(find "$d" \( -type f -o -type d -o -type l \) -print -exec touch -c -h {} + | wc -l)
    echo "$(date -u +%FT%TZ) touched $n entries under $d"
done
