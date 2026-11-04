#!/bin/bash
# Match all events with SPEXone and sample the PPE (all members) there.
#   sbatch --account="$PYRO_SLURM_ACCOUNT" scripts/slurm/match_spexone.sbatch \
#          "$PYRO_DATA_ROOT/events/<table>.csv" [--ppe KEY ... [--allow-coarse]]
# Default source: ybhatti_pd_3h (config/ppe_sources.toml).
#SBATCH --job-name=pyro-match
#SBATCH --partition=rome
#SBATCH --time=02:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --output=slurm-%x-%j.out

set -euo pipefail
if [[ -f config/local.env ]]; then set -a; source config/local.env; set +a; fi
py="${PYRO_PYTHON:-.venv/bin/python}"
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
export HDF5_USE_FILE_LOCKING=FALSE

"$py" -m pyro.match "${1:?event table}" --model "${@:2}"
