#!/bin/bash
# Register a Jupyter kernel that runs inside the pyro Apptainer image, so
# notebooks opened in Cursor / VS Code / JupyterLab on Snellius execute in the
# container (same Python, ipykernel and libraries as the batch jobs).
#
#   scripts/apptainer_kernel.sh install [path/to/pyro.sif]   # default ./pyro.sif
#   scripts/apptainer_kernel.sh remove
#
# Then pick the kernel "pyro (apptainer)" in the notebook. Run on a compute node
# (salloc / Open OnDemand), not on the login node. Data paths outside $HOME are
# bound via PYRO_APPTAINER_BIND (default: /gpfs,/projects,/scratch-shared).
set -euo pipefail
name=pyro-apptainer
kdir="${JUPYTER_DATA_DIR:-$HOME/.local/share/jupyter}/kernels/$name"

case "${1:-install}" in
    remove)
        rm -rf "$kdir"
        echo "removed $kdir"
        ;;
    install)
        sif=$(realpath "${2:-pyro.sif}")
        [[ -f "$sif" ]] || { echo "no image at $sif (build: scripts/apptainer_build.sh)" >&2; exit 1; }
        command -v apptainer >/dev/null || { echo "apptainer not found" >&2; exit 1; }
        apptainer exec "$sif" python -c "import ipykernel" \
            || { echo "$sif has no ipykernel; rebuild the image" >&2; exit 1; }
        bind="${PYRO_APPTAINER_BIND:-/gpfs,/projects,/scratch-shared}"
        repo=$(cd "$(dirname "$0")/.." && pwd)
        mkdir -p "$kdir"
        cat >"$kdir/kernel.json" <<EOF
{
  "argv": ["apptainer", "exec", "--cleanenv", "--bind", "$bind",
           "--env", "PYTHONPATH=$repo/src", "--env", "HDF5_USE_FILE_LOCKING=FALSE",
           "$sif", "python", "-m", "ipykernel_launcher", "-f", "{connection_file}"],
  "display_name": "pyro (apptainer)",
  "language": "python",
  "metadata": {"debugger": true}
}
EOF
        echo "kernel '$name' -> $sif"
        echo "select \"pyro (apptainer)\" in the notebook kernel picker"
        ;;
    *)
        sed -n '2,12p' "$0"; exit 2 ;;
esac
