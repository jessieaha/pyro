#!/bin/bash
# Regional statistics: pyroCb-affected vs background vs all SPEXone scenes per region.
#   sbatch --account="$PYRO_SLURM_ACCOUNT" scripts/slurm/regional_stats.sbatch \
#          "$PYRO_DATA_ROOT/events/<table>.csv" [--ppe KEY ... [--allow-coarse]]
# Default source: ybhatti_pd_3h (config/ppe_sources.toml).
#SBATCH --job-name=pyro-regional
#SBATCH --partition=rome
#SBATCH --time=02:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --output=slurm-%x-%j.out

set -euo pipefail
if [[ -f config/local.env ]]; then set -a; source config/local.env; set +a; fi
py="${PYRO_PYTHON:-.venv/bin/python}"
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
export HDF5_USE_FILE_LOCKING=FALSE

"$py" -m pyro.regional "${1:?event table}" "${@:2}"
