# pyro

This project compares pyroCb and matched non-pyroCb fires in AeroCom and multi-model PPE output against satellite observations. The goal is to quantify the model bias and forcing from missing pyroCb injection, starting with a boreal pilot (Alaska, Canada, Siberia).

Aerosol side: Jessie Zhang (VU Amsterdam). Fire side (event detection and matching): Jelle Kraak (VU Amsterdam).

> **Status:** step 1 in progress. Working: the sync tool (Google Drive / S3 / Research Drive), event-table utilities, event × SPEXone matching with 3-hourly PPE sampling (`pyro-match`), the ICON-HAM PPE parquet store (`pyro-icon`) and in-place parquet queries (`pyro-query`). Corridors, VIIRS download and full collocation are planned in [`docs/pipeline_plan.md`](docs/pipeline_plan.md). All tables use polars.

## Project flowchart

```mermaid
flowchart TD
  subgraph FIRE["Fire side"]
    EV["Boreal pyroCb event table<br/>lon_c/lat_c, onset_utc, injection height, tropopause"]
    CT["Lookalike control fires<br/>matched non-pyroCb fires"]
  end

  subgraph MODEL["Model extraction (Snellius)"]
    PPE["ECHAM-HAM PPE, 251 members<br/>3-hourly, Aug 2024 - Jul 2025"]
    AC["AeroCom 3-hourly 2024-25<br/>(go/no-go)"]
    MM["Multi-model PPE 2025, 6-hourly<br/>context only"]
    ICON["ICON-HAM PPE 2024-25, 392 members<br/>monthly, context only"]
  end

  subgraph OBS["Satellite and ground retrievals"]
    SPEX["PACE SPEXone RemoTAP / OCI<br/>AOD, AE, SSA"]
    VIIRS["VIIRS Deep Blue / Dark Target, NOAA EPS<br/>AOD, AE, smoke mask"]
    ATLID["EarthCARE ATLID profiles<br/>(from Mar 2025)"]
    AERO["AERONET V3"]
  end

  EV --> MATCH["Event x SPEXone match, all events<br/>pyro-match: 150 / 500 / 1000 km rings"]
  SPEX --> MATCH
  PPE -->|"3-hourly, all members"| MATCH
  MATCH --> COL
  ICON --> ICONPQ[("ICON-HAM parquet store<br/>pyro-icon, all variables")]
  EV --> WIN["Event windows and plume corridors<br/>onset -1 d to +10 d, ERA5 trajectories"]
  CT --> WIN
  WIN --> SUB["Model subsets<br/>boreal band, union of event windows"]
  PPE --> SUB
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

## Repository layout

| Path | What |
|---|---|
| `docs/data_inventory.md` | What 2024–25 PPE / AeroCom / observation data exists on Snellius, and the gaps |
| `docs/pipeline_plan.md` | Step 1 plan: event windows, model subsetting, satellite download, collocation to parquet |
| `docs/storage_and_sync.md` | Storage comparison, recommendation, sync tool and DVC usage, licensing rules |
| `docs/environment.md` | Devcontainer / Docker / Apptainer (Snellius, ADA) / Kubernetes |
| `src/pyro/sync.py`, `scripts/sync.sh` | `pyro-sync`: rclone-based, config-driven sync (dry-run default) |
| `src/pyro/events.py` | `pyro-events`: event-table validation, windows, period coverage |
| `src/pyro/match.py` | `pyro-match`: all events × SPEXone pixels, super-obs, 3-hourly PPE sampling (all members) |
| `src/pyro/icon_ppe.py` | `pyro-icon`: ICON-HAM PPE netCDF → partitioned parquet store, with verification |
| `src/pyro/store.py` | `pyro-query`: polars queries on local parquet or in place on Drive via `rclone serve s3` |
| `scripts/gdrive_setup.sh` | Link the VU Google Drive to rclone (interactive, token stays outside the repo) |
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
uv run pyro-match "$PYRO_DATA_ROOT/events/<event_table>.csv"
```

## Data and credentials policy

- No data in git. Event tables, lookalike controls and event-keyed products are **restricted**: keep them under `$PYRO_DATA_ROOT`, sync them only to private remotes, and never publish them before both sides agree. GLD360 lightning data is licensed and never synced.
- No credentials in files. NASA Earthdata, Copernicus ADS/CDS and storage keys come from environment variables or Codespaces/CI secrets (`config/remotes.env.example` lists the names).
- Snellius paths, project and account names go in `config/local.env`, not in committed files.

## Related code

- [AeroCom_Error](https://github.com/jessieaha/AeroCom_Error): PPE/AeroCom readers and the emission × lifetime × MEC AOD error split (regional / monthly). Step 1 extends this with an event-based layer.
- [CAMEL-PACE](https://github.com/jessieaha/CAMEL-PACE): PACE SPEXone L2 to parquet; the satellite extraction reuses its pattern.
- [PyroCB](https://github.com/JJJellyyy/PyroCB): fire-side event matching (FireAtlas). It defines the event-table conventions.
