# pyro

This project compares pyroCb and matched non-pyroCb fires in AeroCom and multi-model PPE output against satellite observations. The goal is to quantify the model bias and forcing from missing pyroCb injection, starting with a boreal pilot (Alaska, Canada, Siberia).

Aerosol side: Jessie Zhang (VU Amsterdam). Fire side (event detection and matching): Jelle Kraak (VU Amsterdam).

> **Status:** step 1 in progress. Working: the sync tool (Google Drive / S3 / Research Drive), event-table utilities, event × SPEXone matching with PPE sampling (`pyro-match`), a common reader for the ECHAM-HAM and ICON-HAM PPEs (`pyro-ppe`), the ICON-HAM PPE parquet store (`pyro-icon`), in-place parquet queries (`pyro-query`), per-event observation-vs-ensemble comparison (`pyro-compare`, `notebooks/event_comparison.ipynb`) and regional AeroVal statistics for pyroCb vs background scenes (`pyro-regional`). Corridors, VIIRS download and full collocation are planned in [`docs/pipeline_plan.md`](docs/pipeline_plan.md). All tables use polars.

## Project flowchart

```mermaid
flowchart TD
  subgraph FIRE["Fire side"]
    EV["Boreal pyroCb event table<br/>lon_c/lat_c, onset_utc, injection height, tropopause"]
    CT["Lookalike control fires<br/>matched non-pyroCb fires"]
  end

  subgraph MODEL["Model extraction (Snellius)"]
    PPE["ECHAM-HAM PPE (ybhatti), 251 members<br/>3-hourly, Aug 2024 - Jul 2025"]
    AC["AeroCom 3-hourly 2024-25<br/>(go/no-go)"]
    MM["Multi-model PPE 2025, 6-hourly<br/>context only"]
    ICON["ICON-HAM PPE 2024-25 (hjia), 392 members<br/>monthly, context only"]
    ICON6["ICON-HAM PPE2025 (hjia), 308 members<br/>6-hourly, Oct 2024 - Dec 2025, dmftar"]
    ADAPT["Common PPE reader, pyro.ppe<br/>canonical names and units, ppe_source, member"]
  end

  subgraph OBS["Satellite and ground retrievals"]
    SPEX["PACE SPEXone RemoTAP / OCI<br/>AOD, AE, SSA"]
    VIIRS["VIIRS Deep Blue / Dark Target, NOAA EPS<br/>AOD, AE, smoke mask"]
    ATLID["EarthCARE ATLID profiles<br/>(from Mar 2025)"]
    AERO["AERONET V3"]
  end

  EV --> MATCH["Event x SPEXone match, all events<br/>pyro-match: 150 / 500 / 1000 km rings"]
  SPEX --> MATCH
  PPE --> ADAPT
  ICONPQ --> ADAPT
  ICON6 -->|"read in place"| ADAPT
  ADAPT -->|"3-hourly, all members"| MATCH
  ADAPT -.->|"6-hourly / monthly, --allow-coarse"| MATCH
  MATCH --> COL
  MATCH --> CMP["Per-event comparison<br/>pyro-compare, notebook: obs vs ensemble"]
  SPEX --> REG["Regional statistics, AeroVal metrics<br/>pyro-regional: all vs background vs pyroCb"]
  EV --> REG
  ADAPT -->|"all members"| REG
  CMP --> AGG
  REG --> AGG
  ICON --> ICONPQ[("ICON-HAM parquet store<br/>pyro-icon, all variables")]
  EV --> WIN["Event windows and plume corridors<br/>onset -1 d to +10 d, ERA5 trajectories"]
  CT --> WIN
  WIN --> SUB["Model subsets<br/>boreal band, union of event windows"]
  ADAPT --> SUB
  AC --> SUB
  WIN --> DL["Satellite download<br/>earthaccess, NOAA CLASS"]
  SPEX --> DL
  VIIRS --> DL
  ATLID --> DL
  AERO --> DL
  SUB --> COL[("Collocation parquet<br/>obs x model x member")]
  DL --> COL
  COL --> AGG["Per-event aggregation<br/>pyroCb vs control, aged downwind plumes"]
  AGG --> ATT["Bias attribution<br/>emission x lifetime x MEC"]
  ATT --> PROXY["pyroCb injection proxy<br/>cloud structure, atmosphere and fire predictors"]
  PROXY --> RERUN["Model rerun with pyroCb injection<br/>ECHAM-HAM incl. PPE subset"]
  RERUN --> FORC["Forcing bias with uncertainty<br/>PPE and multi-model spread"]
  RERUN -.->|"re-evaluate"| COL

  subgraph STORE["Storage and sync layer"]
    SNEL[("Snellius project space<br/>raw model output, compute")]
    SCR[("scratch-shared working copy<br/>keep-alive job every 12 d")]
    GD[("VU Google Drive, 1 TB<br/>shared parquet store, DVC default remote")]
    S3[("SURF Object Store, S3<br/>upgrade path beyond 1 TB")]
    RD[("SURF Research Drive<br/>exchange with fire side")]
  end

  SNEL --- PPE
  ICONPQ --- SCR
  SUB -.->|"pyro-sync"| GD
  ICONPQ -.->|"pyro-sync"| GD
  COL -.->|"pyro-sync / dvc push"| GD
  AGG -.->|"pyro-sync / dvc push"| GD
  GD -.->|"pyro-query in place"| AGG
  GD -.->|"if > 1 TB"| S3
  EV -.->|"restricted, private remotes only"| RD
```

TROPOMI aerosol products are not used.

## How the code runs

One recipe (`Dockerfile`) and one pinned package list (`uv.lock`) produce one environment. GitHub builds it into an image; a laptop runs that image with Docker; Snellius has no Docker, so Apptainer converts the same image into a single file and runs it. The full guide, written for someone new to containers, is [`docs/environment.md`](docs/environment.md).

```mermaid
flowchart TD
  src["Dockerfile + uv.lock in git"]
  ci["GitHub Actions builds the image on main"]
  reg[("ghcr.io/jessieaha/pyro:latest")]
  lap["Laptop / Codespaces / Cursor<br/>Docker or the devcontainer"]
  sif["Snellius / ADA: apptainer build → pyro.sif<br/>Slurm runs commands inside it"]
  venv["Or skip the container:<br/>uv sync into .venv, same lock file"]

  src --> ci --> reg
  reg --> lap
  reg --> sif
  src --> venv
```

The image holds Python, the libraries and tools (`cdo`, `rclone`). It does not hold data or passwords: GPFS folders are handed in with `--bind`, and paths live in the gitignored `config/local.env`. On Snellius today the `.venv` route is enough; the `.sif` route is for batch jobs that must match the published image exactly.

## Repository layout

| Path | What |
|---|---|
| `docs/data_inventory.md` | What 2024–25 PPE / AeroCom / observation data exists on Snellius, and the gaps |
| `docs/pipeline_plan.md` | Step 1 plan: event windows, model subsetting, satellite download, collocation to parquet |
| `docs/storage_and_sync.md` | Storage comparison, recommendation, sync tool and DVC usage, licensing rules |
| `docs/environment.md` | Container setup explained from scratch: Docker, devcontainer, Apptainer on Snellius, `.venv` |
| `src/pyro/sync.py`, `scripts/sync.sh` | `pyro-sync`: rclone-based, config-driven sync (dry-run default) |
| `src/pyro/events.py` | `pyro-events`: event-table validation, windows, period coverage |
| `src/pyro/match.py` | `pyro-match`: all events × SPEXone pixels, super-obs, PPE sampling through `pyro.ppe` (all members) |
| `src/pyro/ppe.py`, `config/ppe_sources.toml` | `pyro-ppe`: one reader/adapter for both PPEs (ybhatti ECHAM-HAM, hjia ICON-HAM) with a common schema |
| `src/pyro/compare.py`, `notebooks/event_comparison.ipynb` | `pyro-compare`: per-event SPEXone super-obs vs ensemble median / 5–95 % / control, metrics and figures |
| `src/pyro/regional.py`, `config/regions.toml` | `pyro-regional`: AeroVal statistics per region for the regional average, background and pyroCb-affected scenes, per member |
| `src/pyro/icon_ppe.py` | `pyro-icon`: ICON-HAM PPE netCDF → partitioned parquet store, with verification |
| `src/pyro/store.py` | `pyro-query`: polars queries on local parquet or in place on Drive via `rclone serve s3` |
| `scripts/gdrive_setup.sh` | Link the VU Google Drive to rclone, or repair a remote saved without token (token stays outside the repo) |
| `scripts/apptainer_kernel.sh` | Jupyter kernel that runs inside the Apptainer image |
| `scripts/scratch_keepalive.sh` | Single self-resubmitting job that refreshes scratch every 12 days |
| `config/` | `sync.toml` (datasets, remotes), `*.example` templates; `local.env` is gitignored |
| `Dockerfile`, `.devcontainer/`, `containers/`, `k8s/` | Portable environment |
| `.dvc/` | DVC remotes (placeholders; real URLs go in `.dvc/config.local`) |

## Quick start

```bash
uv sync --all-extras                      # or: open in Codespaces / devcontainer
cp config/local.env.example config/local.env   # machine paths only, gitignored
uv run pytest
uv run pyro-events coverage "$PYRO_DATA_ROOT/events/<event_table>.csv"
scripts/sync.sh remotes                   # which storage remotes are configured
scripts/gdrive_setup.sh                   # once: link the VU Google Drive
scripts/sync.sh push icon_ppe -r gdrive   # dry run
uv run pyro-ppe list                      # configured PPE sources
uv run pyro-match "$PYRO_DATA_ROOT/events/<event_table>.csv"
uv run pyro-compare                       # per-event figures from the pyro-match output
uv run pyro-regional "$PYRO_DATA_ROOT/events/<event_table>.csv"   # regional statistics
```

## Data and credentials policy

- No data in git. Event tables, lookalike controls and event-keyed products are **restricted**: keep them under `$PYRO_DATA_ROOT`, sync them only to private remotes, and never publish them before both sides agree. GLD360 lightning data is licensed and never synced.
- No credentials in files. NASA Earthdata, Copernicus ADS/CDS and storage keys come from environment variables or Codespaces/CI secrets (`config/remotes.env.example` lists the names).
- Snellius paths, project and account names go in `config/local.env`, not in committed files.

## Related code

- [AeroCom_Error](https://github.com/jessieaha/AeroCom_Error): PPE/AeroCom readers and the emission × lifetime × MEC AOD error split (regional / monthly). Step 1 extends this with an event-based layer.
- [CAMEL-PACE](https://github.com/jessieaha/CAMEL-PACE): PACE SPEXone L2 to parquet; the satellite extraction reuses its pattern.
- [PyroCB](https://github.com/JJJellyyy/PyroCB): fire-side event matching (FireAtlas). It defines the event-table conventions.
