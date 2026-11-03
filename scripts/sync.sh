#!/usr/bin/env bash
# Run pyro-sync with any Python >= 3.11 (stdlib only) plus rclone.
#   scripts/sync.sh remotes
#   scripts/sync.sh push model_subsets -r surf_s3            # dry run
#   scripts/sync.sh push model_subsets -r surf_s3 --apply --slurm
# Falls back to the container (PYRO_SIF=/path/to/pyro.sif) if no suitable
# Python is found; Slurm submission (--slurm) needs the host route.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYRO_SYNC_CONFIG="${PYRO_SYNC_CONFIG:-$here/config/sync.toml}"
export PYRO_SYNC_ENV_FILE="${PYRO_SYNC_ENV_FILE:-$here/config/local.env}"
export PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}"

for py in "$here/.venv/bin/python" python3.12 python3.11 python3.13 python3; do
  if command -v "$py" >/dev/null 2>&1 &&
     "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$py" -m pyro.sync "$@"
  fi
done

if [[ -n "${PYRO_SIF:-}" ]]; then
  exec apptainer exec --bind "${PYRO_BIND:-$here}" "$PYRO_SIF" python -m pyro.sync "$@"
fi

echo "sync.sh: need Python >= 3.11 or PYRO_SIF pointing at the pyro container" >&2
exit 1
