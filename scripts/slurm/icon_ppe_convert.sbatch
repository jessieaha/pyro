#!/bin/bash
# Convert the ICON-HAM PD PPE netCDF files to the parquet store and verify it.
#   source config/local.env   # or export the variables below
#   sbatch --account="$PYRO_SLURM_ACCOUNT" scripts/slurm/icon_ppe_convert.sbatch
# Reads ICON_PPE_SOURCE_ROOT (read-only) and writes ICON_PPE_STORE.
#SBATCH --job-name=pyro-icon-ppe
#SBATCH --partition=rome
#SBATCH --time=02:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --output=slurm-%x-%j.out

set -euo pipefail
if [[ -f config/local.env ]]; then set -a; source config/local.env; set +a; fi
: "${ICON_PPE_SOURCE_ROOT:?}"
: "${ICON_PPE_STORE:?}"
py="${PYRO_PYTHON:-.venv/bin/python}"
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
export HDF5_USE_FILE_LOCKING=FALSE OMP_NUM_THREADS=1

"$py" -m pyro.icon_ppe convert --workers "${SLURM_CPUS_PER_TASK:-8}" \
    --workdir "${TMPDIR:-/tmp}"
"$py" -m pyro.icon_ppe verify -n 48
du -sh "$ICON_PPE_STORE"
