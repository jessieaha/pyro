#!/bin/bash
# Generic Slurm wrapper for one pipeline step inside the pyro container.
#   sbatch --account="$PYRO_SLURM_ACCOUNT" scripts/slurm/run_step.sbatch \
#          python -m pyro.events coverage "$PYRO_DATA_ROOT/events/<table>.csv"
# Partition/time/cpus can be overridden on the sbatch command line.
#SBATCH --job-name=pyro-step
#SBATCH --partition=rome
#SBATCH --time=02:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --output=slurm-%x-%j.out

set -euo pipefail
: "${PYRO_SIF:?set PYRO_SIF to the pyro.sif path}"
: "${PYRO_DATA_ROOT:?set PYRO_DATA_ROOT (see config/local.env.example)}"

# Read-only model sources plus the writable data root. Extend PYRO_BIND for more.
bind="${PYRO_BIND:-/projects,/gpfs,/scratch-shared}"
export HDF5_USE_FILE_LOCKING=FALSE
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"

exec apptainer exec --bind "$bind" "$PYRO_SIF" "$@"
