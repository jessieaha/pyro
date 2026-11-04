# Pipeline plan. Step 1: prepare 2024–25 model data for comparison with PACE SPEXone and VIIRS around pyroCb events

Scope: boreal pilot covering **all events in the table**: Alaska, Canada and Siberia / Northern Asia, kept and not kept (the `kept` flag is carried through, not used to drop rows). Model: **3-hourly output only** is matched to satellite overpasses: the PD 2024–25 ECHAM-HAM PPE `hifreq` fields (`ppe_source = ybhatti`), plus AeroCom 3-hourly models if the go/no-go check passes. Both 2024–25 PPEs, ECHAM-HAM (`ybhatti`) and ICON-HAM (`hjia`), are read through one adapter with a common schema (§1b′). The ICON-HAM PPE has no 3-hourly output: its 6-hourly PPE2025 starts after the 2024 events and its 2024–25 run is monthly, so ICON-HAM and other coarser output (MM PPE 6-hourly, monthly streams) are context only. They are collocated only on explicit request (`--allow-coarse`) and are always labelled by `resolution`. Observations for step 1 are PACE SPEXone (AOD, SSA, AE; RemoTAP) and VIIRS (AOD, AE; Deep Blue / Dark Target, and NOAA EPS). Step 1 stops at collocated parquet. Per-event aggregation against lookalike controls is outlined in §3 only.

What data exists and where the gaps are: [`data_inventory.md`](data_inventory.md). Where data lives and how it moves: [`storage_and_sync.md`](storage_and_sync.md). How to run the code anywhere: [`environment.md`](environment.md).

Hard rules:
- Collocate on the **matched fire position `lon_c`/`lat_c`**, never the reported, rounded `lon`/`lat`.
- Use timestamps in UTC (`onset_utc`).
- **No TROPOMI aerosol products.**
- Credentials come from environment variables only (`EARTHDATA_*`, `ADS_API_*`, `CDS_API_*`, `AWS_*`). Never ask for them in code, write them to files or log them.
- Event tables and anything keyed by event are restricted data. Store them under `$PYRO_DATA_ROOT`, never in git; `pyro-sync` enforces this.
- Tables are handled with **polars** (lazy scans, predicate/projection pushdown) and stored as parquet; netCDF is only read, at the model side.

## 0. Inputs and go/no-go

| Item | Owner | Status | Action |
|---|---|---|---|
| 2024 boreal pyroCb event table | fire side | received (CSV) | Keep as external data in `$PYRO_DATA_ROOT/events/`, validated by `pyro-events` |
| **2025 boreal event table** | fire side | to request | Most of the PPE period is 2025, and the MM PPE covers only 2025 (inventory §5) |
| Lookalike control table | fire side | to request | Same schema plus `role` and `pair_id` (below) |
| AeroCom 3-hourly models, Aug 2024–Jul 2025 | aerosol side | **unverified** | List per model on the MET Norway AeroCom server before building the event-side pipeline |
| Decide how to cover Jun–Jul 2024 | both | open | Options: 2025 events, a spin-up rerun with 3-hourly output, or an Aug–Sep-only pilot |

**Event table contract.** These columns are required by `pyro.events.load_events`:

| Column | Type | Notes |
|---|---|---|
| `pcb_id` | str, unique | Event id |
| `onset_utc` | datetime (UTC) | Onset time |
| `region` | str | Alaska / Canada / Northern Asia |
| `lon_c`, `lat_c` | float | Matched fire position; **used for collocation** |
| `kept` | bool | Rows with `False` are excluded by default |
| optional | — | `lon`, `lat` (reported), `move_km`, `pos_source`, `radius_km`, `status`, `is_confirmed`, `mergid`, `fire_name`, `max_inject_alt` (m), `trop_alt` (m) |
| controls (planned) | — | `role` ∈ {`pyrocb`, `control`}, `pair_id`, plus the fire-side emission proxies (summed FRP in MW and MW/km², `frp_source`, a saturation/missing flag) |

## 1a. Event windows and plume corridors

```bash
pyro-events coverage "$PYRO_DATA_ROOT/events/pcb_events_2024_boreal.csv"     # overlap with each data period
pyro-events windows  "$PYRO_DATA_ROOT/events/pcb_events_2024_boreal.csv" \
    -o "$PYRO_DATA_ROOT/derived/event_windows/pcb_events_2024_boreal.parquet" --pre-days 1 --post-days 10
```

- **Window:** onset − 1 day to onset + 10 days. PyroCbs mostly form in the afternoon, after the ~13:30 local-time overpasses of PACE and VIIRS. The same-day overpass is therefore usually a pre-onset background scene, and the first post-onset retrieval comes the next day.
- **Why downwind aged plumes (days 1–10) are the main target.** A pyroCb scene is cloudy by definition, while control fires are sampled in clearer scenes, so comparing source scenes would mix up cloud screening with smoke. Both pyroCb and control plumes are instead compared at the same plume age, away from the source, with identical screening applied to observations and model (§1d).
- **Corridors** (module `pyro.steps.corridors`, planned). Forward kinematic trajectories start from `lon_c`/`lat_c` at onset at three heights:
  1. `max_inject_alt` (pyroCb top),
  2. `trop_alt` − 1 km (the UTLS layer),
  3. about 2 km (where the model injects smoke).

  Winds come from ERA5 hourly pressure levels (CDS, via `cdsapi`). The PPE is nudged to ERA5, so its winds match. Each day of plume age gets a buffered corridor polygon (radius growing with age, e.g. 100 km + 50 km/day). Satellite pixels are tagged with `corridor` ∈ {`source`, `pbl`, `utls`, `top`} and `age_days`. Observed smoke along the UTLS corridor with too little model AOD there is the expected pyroCb signal.
- Output: `derived/event_windows/*.parquet` (restricted).

## 1b. Model subsetting (on Snellius only)

The input never leaves Snellius. Only the subsets are synced.

| Source | Variables | Resolution |
|---|---|---|
| Stacked PD files (`PPE_STACKED_ROOT`) | `TAU_2D_550nm`, `ABS_2D_550nm`, `SSA_2D_550nm`, `ANG_440nm_670nm`, `TAU_2D_MODE_{KS,AS,CS,KI,AI,CI}_550nm`, `lcc` | 3 h, 251 members |
| Per-member `*_hifreq.nc` (`PPE_SOURCE_ROOT`) | `ANG_550nm_865nm`, `TAU_3D_355nm`, `burden_{BC,OC,SO4,SS,DU,WAT}`, `aclcov`, `aps`, `iccl` | 3 h |
| Per-member `*_rad.nc` | `TAU_COMP_{BC,OC,SO4,SS,WAT}_550nm` | daily |
| Emissions | GFAS daily input × member `SCALE_EMI_BB_*`, checked against monthly `emi_*_tfire` | daily |
| AeroCom (if go) | `od550aer`, `od440aer`/`od870aer` or `abs550aer`, `ec550aer` / `ec532aer` where available | 3 h |

- **Subset in time and space:** the time union of all event windows, on a 40–80° N band at all longitudes so downwind plumes stay inside. Per-event longitude sectors are enough for the 47-level profile. Expected sizes are in inventory §7: about 18 GB for 2D and about 90 GB with profiles, for the Aug–Sep 2024 events.
- **No regridding of the model.** Keep the native T63 Gaussian grid. For gridded satellite products, aggregate the satellite data *to the model grid* (super-observations per cell and overpass, with count and standard deviation). Interpolating a 1.9° model to satellite pixels does not add information and makes representativeness errors worse.
- **Derived fields:** SSA = 1 − ABS/TAU (as a check on `SSA_2D_550nm`), AAOD, fine-mode fraction from the mode AODs, and layer extinction from `TAU_3D_355nm`. Layer thickness comes from `aps`, the hybrid coefficients and the hypsometric equation with ERA5 temperature; the monthly `grheightm1` is a fallback.
- **Format:** parquet, hive-partitioned by month (`month=YYYY-MM/part-*.parquet`), one row per (member, 3-hourly step, cell) with float32 value columns, rows sorted by latitude so `lat`/`lon` filters skip row groups, files ≥ 50 MB (Google Drive handles few large files well, many small ones badly). Profiles (`TAU_3D_355nm`) stay netCDF4 per (event sector, month). A `provenance.csv` in the AeroCom_Error style (source file, variable, status) goes alongside. The ICON-HAM PPE store (`pyro-icon convert`, inventory §8) uses the same layout.
- **Execution:** a Slurm array on `rome` over (variable group × month), inside the container (`scripts/slurm/run_step.sbatch`). This is I/O-bound: each task reads contiguous (member, step) slices, so keep 4–8 tasks at a time to protect GPFS.
- Module: `pyro.steps.model_subset` (planned). It should reuse `AeroCom_Error/py/PPE_set.py`, which knows the run locations and variable mappings.

## 1b′. Common PPE reader (implemented: `pyro.ppe`, `pyro-ppe`)

Both PPEs are read in place through one adapter, so collocation code never sees model-specific names, grids or time encodings. Sources are declared in `config/ppe_sources.toml` (paths only via `${VAR}` from `config/local.env`):

| Key | `ppe_source` | Reader | Resolution | Use in step 1 |
|---|---|---|---|---|
| `ybhatti_pd_3h` | ybhatti | `echam_stacked`: stacked `(ensemble, time, lat, lon)` files, per-member `*_hifreq.nc` fallback for unstacked variables | 3 h | **primary**, satellite matching |
| `hjia_pd2025_6h` | hjia | `icon_per_variable` with `archive = true`: reads netCDF4 members directly inside the dmftar tar files (offsets from the `.idx` listing, header checked) | 6 h | 2025 events (none in 2024) |
| `hjia_pd_2024_25_monthly` | hjia | `icon_parquet`: the `pyro-icon` parquet store | monthly mean | context, budgets, deposition |
| `hjia_pd_2024_25_monthly_nc` | hjia | `icon_per_variable` on the per-member netCDF source | monthly mean | same, while the source exists |

`ppe.sample(source, points, variables)` takes a polars table of `point_id, time (UTC), lat, lon` and returns one row per (point, member):

`point_id, ppe_source, dataset, model, resolution, member, is_control, t_idx, model_time, dt_hours, cell, model_lat, model_lon, <canonical variables>`

- **Variables** use AeroCom-style canonical names and units (`od550aer`, `abs550aer`, `ae440_670`, `ae550_865`, `od550bc…`, `load*` kg m⁻², `emi*`/`dry*`/`pr` kg m⁻² s⁻¹); `ssa550aer` is always derived as 1 − AAOD/AOD. A variable a source does not have raises an error instead of returning silent NaNs; `sample_model` in `pyro-match` drops it from that source's output.
- **Time:** nearest step for instantaneous output (outside ±half a step → `t_idx = −1`, null values); for means the containing interval, with `model_time` at its midpoint and the label convention (`end`/`start`) set per source. No interpolation in time or space.
- **Space:** nearest grid cell; `cell` is the T63 index `j·192 + i` or the ICON cell number; longitudes in [−180, 180).
- **Members:** `member` is the native ID (ECHAM: 0 = Control; ICON: `ens_NNN`), `is_control` is true only for the ECHAM control. Downstream code groups by (`ppe_source`, `member`) and never mixes the two ensembles' IDs.
- Tests with synthetic ECHAM, ICON netCDF, ICON dmftar and parquet samples check that all readers return the same schema and values (`tests/test_ppe.py`). On the real data the adapter reproduces the earlier direct reads of the stacked ECHAM files exactly, and the ICON parquet store and its netCDF source return identical values.

## 1c. Satellite download

Authentication: `earthaccess.login(strategy="environment")` reads `EARTHDATA_TOKEN`, or `EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD`. Jessie supplies these as Codespaces/CI secrets or exports them in the shell or Slurm job. Do not write a `.netrc`; it is gitignored anyway. Copernicus access uses `cdsapi.Client(url=os.environ["CDS_API_URL"], key=os.environ["CDS_API_KEY"])` for ERA5 on the **CDS**, and the same with `ADS_API_*` for CAMS on the **ADS**.

| Product | Source / short name | Variables | QA filter (first pass) | Notes |
|---|---|---|---|---|
| PACE SPEXone RemoTAP L2 v4.0 | earthaccess: `PACE_SPEXONE_L2_AER_RTAPLAND`, `PACE_SPEXONE_L2_AER_RTAPOCEAN` | `aot` (12 λ, 340–865 nm), `ssa`, `fmf`, `reff_*`, `mr`/`mi`, uncertainties, `chi2` | 0.05 ≤ AOT550 ≤ 5, χ² ≤ 5 (CAMEL-PACE) | Local extracts cover 2024-06 → 2025-12. Extend the CAMEL-PACE loader with per-pixel time, QA flags and V4 layer height. The narrow swath gives sparse revisits; this is the key SSA constraint |
| PACE OCI UAA (optional) | `PACE_OCI_L2_AER_UAA` v3.1 | AOD, UV aerosol index | product QA | Wide swath; cross-check for SPEXone sampling |
| VIIRS Deep Blue L2 v2.1 | `AERDB_L2_VIIRS_{SNPP,NOAA20,NOAA21}` | `Aerosol_Optical_Thickness_550_Land_Ocean_Best_Estimate`, `Angstrom_Exponent_Land_Ocean_Best_Estimate`, aerosol type (smoke) | best-estimate (QA-filtered) fields | Over land, AE uses a different wavelength pair than the model (see §1d) |
| VIIRS Dark Target L2 v2.1 | `AERDT_L2_VIIRS_{SNPP,NOAA20,NOAA21}` | `Optical_Depth_Land_And_Ocean`, AE over ocean | QA-filtered combined field | Mostly a second opinion over land |
| NOAA EPS VIIRS AOD + ADP | NOAA JRR-AOD / JRR-ADP | AOD550, AE pairs, ADP smoke and dust flags | high-quality QC only | The NOAA open-data buckets on AWS are a **rolling archive** (only 2026 present on 2026-11-03), so 2024–25 must be ordered from NOAA CLASS. The smoke mask helps with dense plumes |
| ERA5 pressure levels | CDS `reanalysis-era5-pressure-levels` | u, v, t, z (1000–100 hPa), hourly | — | For trajectories and model layer heights |
| CAMS (optional) | ADS | AOD reference | — | Context only |

Later steps only, not step 1: OMPS-LP L2 aerosol (`OMPS_NPP_LP_L2_AER_DAILY` v2.5, `OMPS_N21_LP_L2_AER_DAILY`), EarthCARE ATLID L2 from ESA (public from 11 Mar 2025), and AERONET V3 through the web service. The local copies are listed in the inventory.

How the download works (module `pyro.steps.fetch_satellite`, planned):
- One CMR query per event with the window as temporal filter and the corridor envelope as bounding box. This avoids pulling whole days.
- Granules are cached in `raw/satellite/<product>/<yyyy>/<doy>/`.
- Each granule is extracted to parquet straight away, following the CAMEL-PACE pattern: valid pixels only, one row per pixel, with columns `lat, lon, time_utc, <var>_<λ>nm, <var>_uncertainty_<λ>nm, qa_*`. Raw granules can be deleted after extraction because they can be downloaded again.
- Restartable: a granule whose parquet output exists is skipped.

## 1d. Collocation into parquet

Module `pyro.steps.collocate` (planned). The layout is hive-partitioned and readable with `pyarrow.dataset`, DuckDB or polars:

```
derived/collocation/
  obs/product=<p>/event=<pcb_id>/part-*.parquet     # one row per satellite pixel (or super-obs)
  model/product=<p>/model=<m>/event=<pcb_id>/part-*.parquet
```

| Table | Columns |
|---|---|
| `obs` | `obs_id`, `product`, `pcb_id`, `role`, `pair_id`, `time_utc`, `lat`, `lon`, `age_days`, `corridor`, `dist_km`, `aod550`, `ae_440_670`, `ae_550_865`, `ae_native`, `ae_native_pair`, `ssa550`, uncertainties, `qa`, `smoke_flag` |
| `model` | `obs_id`, `model`, `member`, `aod550`, `ae_440_670`, `ae_550_865`, `ssa550`, `aaod550`, `fmf550`, `cloud_cover`, `lcc`, `burden_bc`, `burden_oc`, `frac_below2km`, `i_lat`, `i_lon`, `dt_min` |

- **Space:** the model T63 cell that contains the pixel. The main analysis also uses super-obs per (cell, overpass), keeping `n_pix` and `std`.
- **Time:** the nearest 3-hourly model step (at most 1.5 h from the overpass), with `dt_min` recorded. Linear interpolation between the two neighbouring steps is a sensitivity test. No daily, 6-hourly or monthly model fields are used here.
- **Same sampling for model and observations:** the model is sampled only where and when a valid retrieval exists, so cloud-screening effects apply equally to pyroCb and control scenes. Model cloud cover at those samples is stored so that the screening can be checked.
- **Ångström pairs:** SPEXone allows recomputing AE for exactly the model pairs (440–670 and 550–865). VIIRS pairs do not match the model, so they are stored as `ae_native` with `ae_native_pair` and used for pyroCb − control *differences*, where pair mismatch largely cancels.
- **Size:** one row per valid pixel per product. For 251 members the `model` table dominates the volume; partitioning by event keeps files small and lets restricted data be synced per event.

## 1e. Event × SPEXone matching (implemented: `pyro-match`)

A first pass that answers "which events have SPEXone data at all" before the full corridor-based collocation exists.

```bash
pyro-match "$PYRO_DATA_ROOT/events/<table>.csv"                       # all events, SPEXone only
sbatch --account="$PYRO_SLURM_ACCOUNT" scripts/slurm/match_spexone.sbatch \
       "$PYRO_DATA_ROOT/events/<table>.csv"                           # + 3-hourly PPE, all members
# both PPEs, ICON monthly as context:
#   ... match_spexone.sbatch <table>.csv --ppe ybhatti_pd_3h --ppe hjia_pd_2024_25_monthly --allow-coarse
```

- Every event in the table is processed (all regions; `kept` is kept as a column). Window: onset − 1 d to onset + 10 d. Pixels: SPEXone RemoTAP land + ocean extracts within 1000 km of `lon_c`/`lat_c` (great-circle distance, dateline-safe).
- `summary.parquet/csv`: per event, pixel and overpass-day counts within 150 / 500 / 1000 km; pixels within 150 km between −1 d and +2 d (`n_pix_150km_m1d_p2d`); smoke-laden pixels (AOD550 ≥ 0.5) near and within 500 km; first post-onset overpass; median SSA of smoky pixels; whether the onset lies in the 3-hourly PPE period.
- `--model [--ppe KEY ...]` (default `ybhatti_pd_3h`; non-3-hourly sources need `--allow-coarse`): for each source, pixels within 500 km are averaged to super-obs per (event, model step, model cell) with AOD, AAOD = AOD·(1 − SSA), SSA and AE(440–670) recomputed for the model pair. Every member is then sampled through `pyro.ppe` (`od550aer`, `abs550aer`, `ssa550aer`, `ae440_670`, `ae550_865`, where available) in the common schema.
- Outputs (`$PYRO_DATA_ROOT/derived/spexone_event_match/`, restricted): `pixels.parquet`, `summary.parquet`, `summary.csv`, and per source `superobs_<key>.parquet` and `model_<key>.parquet`.
- First run on the 2024 boreal table: every region has SPEXone matches, including all Alaska events. The only events without any SPEXone pixel are the earliest ones whose whole window falls in May 2024, before the local SPEXone extracts start (June 2024). About half the events have smoke-laden pixels within 150 km in the first two days. Only events from August onwards can be compared with the 3-hourly PPE.
- Caveats: distance rings are not plume corridors (§1a), so a smoky pixel 500 km away may belong to another fire; the AOD threshold is a screening aid, not attribution. The PPE period starts 2024-08-01, so June–July events have SPEXone matches but no 3-hourly model counterpart (inventory §6).

## 2. Where each step runs

| Step | Runs on | Reads | Writes | Synced? |
|---|---|---|---|---|
| 1a windows / corridors | anywhere (container) | event table, ERA5 | `derived/event_windows` | restricted, private remotes only |
| 1b model subset | **Snellius only** (`rome`, Slurm array) | PPE / AeroCom files | `derived/model_subsets` | yes, to the object store |
| 1c satellite download | Snellius (`staging`) or anywhere | NASA / NOAA / Copernicus | `raw/satellite` | optional (re-downloadable) |
| 1d collocation | Snellius or anywhere with the subsets | 1a–1c | `derived/collocation` | restricted |
| 1e event × SPEXone match | Snellius (`rome`, one job) | event table, SPEXone extracts, PPE sources via `pyro.ppe` | `derived/spexone_event_match` | restricted (private Drive / S3) |
| sync | Snellius `staging` or anywhere | — | remotes | `pyro-sync` / DVC |

## 3. Later: per-event aggregation (outline only)

1. For each event and plume-age bin, and for each model or member: mean model − observation error in AOD, AE and SSA (and its log ratio), with counts.
2. **pyroCb vs control.** Take paired differences of those errors (pyroCb minus matched lookalike control). Controls are matched in a fire-power band, and emitted-mass proxies are recorded so that emissions can be adjusted for in the analysis rather than matched on. Same-fire controls serve as a sensitivity test.
3. **Attribution.** Split the AOD error into emission × lifetime × MEC terms (Zhong et al. 2022), reusing AeroCom_Error. Emission comes from daily GFAS × scale factor in the source box, lifetime from burden decay along the corridor, and MEC = AOD / burden.
4. **Uncertainty.** Combine PPE spread (251 members, optionally emulator-weighted), retrieval uncertainty, sampling (bootstrap over events and pixels) and cloud-mask sensitivity.
5. Outputs go to `derived/event_aggregates` (restricted). The proxy, the rerun with pyroCb injection, and the forcing bias follow in later steps (see the flowchart in the README).
