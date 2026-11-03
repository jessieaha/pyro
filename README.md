# pyro

This project compares pyroCb and matched non-pyroCb fires in AeroCom and multi-model PPE output against satellite observations. The goal is to quantify the model bias and forcing from missing pyroCb injection, starting with a boreal pilot (Alaska, Canada, Siberia).

Aerosol side: Jessie Zhang (VU Amsterdam). Fire side (event detection and matching): Jelle Kraak (VU Amsterdam).

> **Status:** planning and scaffold. Step 1 is model and satellite preparation around the 2024 boreal event list. The sync tool and the event-table utilities work; the extraction, download and collocation modules are planned in [`docs/pipeline_plan.md`](docs/pipeline_plan.md).

## Project flowchart

```mermaid
flowchart TD
  subgraph FIRE["Fire side"]
    EV["Boreal pyroCb event table<br/>lon_c/lat_c, onset_utc, injection height, tropopause"]
    CT["Lookalike control fires<br/>matched non-pyroCb fires"]
  end

  subgraph MODEL["Model extraction (Snellius)"]
    PPE["ECHAM-HAM PPE, 251 members<br/>3-hourly, Aug 2024 - Jul 2025"]
    MM["Multi-model PPE 2025<br/>6-hourly"]
    AC["AeroCom 3-hourly 2024-25<br/>(go/no-go)"]
  end

  subgraph OBS["Satellite and ground retrievals"]
    SPEX["PACE SPEXone RemoTAP / OCI<br/>AOD, AE, SSA"]
    VIIRS["VIIRS Deep Blue / Dark Target, NOAA EPS<br/>AOD, AE, smoke mask"]
    ATLID["EarthCARE ATLID profiles<br/>(from Mar 2025)"]
    AERO["AERONET V3"]
  end

  EV --> WIN["Event windows and plume corridors<br/>onset -1 d to +10 d, ERA5 trajectories"]
  CT --> WIN
  WIN --> SUB["Model subsets<br/>boreal band, union of event windows"]
  PPE --> SUB
  MM --> SUB
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
    S3[("SURF Object Store, S3<br/>shared, DVC remote")]
    RD[("SURF Research Drive<br/>fallback, exchange")]
  end

  SNEL --- PPE
  SUB -.->|"pyro-sync"| S3
  COL -.->|"pyro-sync / dvc push"| S3
  AGG -.->|"pyro-sync / dvc push"| S3
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
scripts/sync.sh push model_subsets -r surf_s3   # dry run
```

## Data and credentials policy

- No data in git. Event tables, lookalike controls and event-keyed products are **restricted**: keep them under `$PYRO_DATA_ROOT`, sync them only to private remotes, and never publish them before both sides agree. GLD360 lightning data is licensed and never synced.
- No credentials in files. NASA Earthdata, Copernicus ADS/CDS and storage keys come from environment variables or Codespaces/CI secrets (`config/remotes.env.example` lists the names).
- Snellius paths, project and account names go in `config/local.env`, not in committed files.

## Related code

- [AeroCom_Error](https://github.com/jessieaha/AeroCom_Error): PPE/AeroCom readers and the emission × lifetime × MEC AOD error split (regional / monthly). Step 1 extends this with an event-based layer.
- [CAMEL-PACE](https://github.com/jessieaha/CAMEL-PACE): PACE SPEXone L2 to parquet; the satellite extraction reuses its pattern.
- [PyroCB](https://github.com/JJJellyyy/PyroCB): fire-side event matching (FireAtlas). It defines the event-table conventions.
