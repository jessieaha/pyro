#!/usr/bin/env bash
# Build (or refresh) the pyro Apptainer image on an HPC login/staging node.
#   scripts/apptainer_build.sh                 # -> $PYRO_SIF or ./pyro.sif from :latest
#   PYRO_IMAGE_TAG=sha-1a2b3c4 scripts/apptainer_build.sh
# Cache and temp go to scratch, not $HOME (200 GiB quota on Snellius).
set -euo pipefail

tag="${PYRO_IMAGE_TAG:-latest}"
image="${PYRO_IMAGE:-ghcr.io/jessieaha/pyro}:${tag}"
out="${PYRO_SIF:-$PWD/pyro.sif}"
scratch="${TMPDIR:-/tmp}"
if [[ -d /scratch-shared && -w "/scratch-shared/$USER" ]]; then
  scratch="/scratch-shared/$USER"
fi
export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$scratch/apptainer-cache}"
export APPTAINER_TMPDIR="${APPTAINER_TMPDIR:-$scratch/apptainer-tmp}"
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"

echo "building $out from docker://$image"
apptainer build --force "$out" "docker://$image"
apptainer exec "$out" python -c "import pyro; print('pyro', pyro.__version__)"
