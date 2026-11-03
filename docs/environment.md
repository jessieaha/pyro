# Portable environment

There is one image definition (`Dockerfile`) and one lock file (`uv.lock`). Codespaces, Cursor, a VM, Snellius and VU ADA all run the same software.

| Where | How |
|---|---|
| GitHub Codespaces / Cursor / VS Code | `.devcontainer/devcontainer.json` (builds the `dev` target) |
| Any VM or laptop with Docker / Podman | `docker build -t pyro .` or `docker run ghcr.io/jessieaha/pyro:latest` |
| Snellius, VU ADA (no Docker) | Apptainer image built from the same OCI image |
| Plain Python | `uv sync` (Python 3.11–3.12) |
| Kubernetes (optional) | `k8s/` |

## Contents

- Python 3.12 (Debian bookworm slim) with locked dependencies from `uv.lock`. Core: numpy, polars, pyarrow. `analysis` extra: xarray, netCDF4, h5netcdf, dask, scipy, pyproj, shapely, earthaccess, cdsapi, s3fs, matplotlib. `dvc` extra: dvc with s3, webdav and ssh support.
- Command-line tools: `cdo`, `nco`, `ncdump` and `rclone`.
- Entry points: `pyro-sync` and `pyro-events`.

### Updating the lock

```bash
uv add <package>            # or edit pyproject.toml, then:
uv lock                     # resolve
uv sync --all-extras        # install locally
uv run pytest
```

CI fails if `uv.lock` is out of date (`uv lock --check`).

## Codespaces / Cursor

Open the repo in a Codespace, or use "Reopen in Container" in Cursor or VS Code. The devcontainer declares the secrets it can use: `EARTHDATA_TOKEN`, `ADS_API_KEY`, `CDS_API_KEY`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Add them as **Codespaces secrets** in GitHub (Settings → Codespaces → Secrets); they then appear as environment variables. Nothing is stored in the repo. Data lives in `./data` (gitignored), filled with `pyro-sync pull …` or `dvc pull`.

## Snellius and VU ADA with Apptainer

No Docker daemon is needed, and Apptainer builds unprivileged from a registry image:

```bash
# once per image version, on a login node; cache goes to scratch, not $HOME
PYRO_SIF=$HOME/containers/pyro.sif scripts/apptainer_build.sh            # from ghcr.io/jessieaha/pyro:latest
# or pin a build: PYRO_IMAGE_TAG=sha-<commit> scripts/apptainer_build.sh

# interactive
apptainer exec --bind /projects,/scratch-shared,/gpfs "$PYRO_SIF" pyro-events coverage "$PYRO_DATA_ROOT/events/<table>.csv"

# batch: generic wrapper (rome partition, account from your environment)
sbatch --account="$PYRO_SLURM_ACCOUNT" scripts/slurm/run_step.sbatch python -m pyro.events coverage "$PYRO_DATA_ROOT/events/<table>.csv"
```

Notes:
- **Do not `module load 2025`** (or other toolchains) before running the container. The container brings its own Python, HDF5, netCDF and CDO. Host modules leak into `PATH`/`LD_LIBRARY_PATH` and break the libraries.
- Apptainer passes the host environment through by default. That is how `EARTHDATA_TOKEN`, `RCLONE_CONFIG_*` and the other variables reach the container. Avoid `--cleanenv` unless you pass them explicitly with `--env`.
- `HDF5_USE_FILE_LOCKING=FALSE` is set in `containers/pyro.def` and `run_step.sbatch`, because netCDF4 on GPFS otherwise fails intermittently on open.
- GHCR packages start out private. Either make `ghcr.io/jessieaha/pyro` public in the package settings (it contains no data or secrets), or log in first with `apptainer remote login --username <user> docker://ghcr.io` using a read-only token.
- `containers/pyro.def` wraps the OCI image with HPC defaults and an `%test` section (`apptainer test pyro.sif`).
- The sync tool needs only Python ≥ 3.11 and rclone. On Snellius, `scripts/sync.sh` finds a suitable Python (the repo `.venv` or `python3.12`) without the container, which is also the route to use for `--slurm` submission.

## Images and CI

- `.github/workflows/ci.yml` runs lint, tests (including an end-to-end rclone test against a local remote) and a lock check.
- `.github/workflows/container.yml` builds the image on every PR that touches the environment and runs a smoke test. On `main` and tags it pushes `ghcr.io/jessieaha/pyro:{latest,sha-<commit>,<tag>}`. For reproducible HPC runs, pin `sha-<commit>`.

## Kubernetes: when it's worth it

See [`../k8s/README.md`](../k8s/README.md). In short, Snellius with Slurm and Apptainer covers step 1, and the model subsetting cannot leave Snellius anyway. Kubernetes only pays off if the satellite and collocation steps move to a cloud or Research Cloud cluster next to the object store, or a service has to run continuously. Sample Jobs per step are provided for that case.
