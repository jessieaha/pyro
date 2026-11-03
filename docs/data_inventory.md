# Data inventory: 2024–25 model output and observations on Snellius

Status as of 3 Nov 2026. The exploration was read-only: directory listings, `ncdump -h` headers and file sizes. No data was copied, moved or changed.

Because this repository is public, locations appear here only as variables. Their values are in `config/local.env`, which is gitignored; `config/local.env.example` has the template.

| Variable | What it points to |
|---|---|
| `PPE_SOURCE_ROOT` | Raw per-member ECHAM-HAM PPE runs (`PPE_Experiments/`, `MM_PPE_Experiments/`, `PI_PPE_Experiments/`) |
| `PPE_STACKED_ROOT` | Ensemble-stacked post-processed PD files (`<var>_hifreq_PPE.nc`, …) |
| `PPE_EXTRACT_ROOT` | Monthly ensemble extracts made with AeroCom_Error (`pd2024/ensemble/`), **on scratch** |
| `AEROCOM_LOCAL_ROOT` | Local copy of AeroCom Phase III files |
| `SPEXONE_EXTRACT_ROOT` | SPEXone RemoTAP L2 parquet extracts (CAMEL-PACE pattern) |
| `ATLID_L3_ROOT` | EarthCARE ATLID gridded extinction |
| `SHARED_OBS_ROOT` | Group-shared observation pool (AERONET, MODIS, MERRA-2, SPEXone gridded, ATLID L3) |
| `ICON_PPE_SOURCE_ROOT` | ICON-HAM PD PPE 2024–25 per-member monthly netCDF, in a **temporary** shared folder (§8) |
| `ICON_PPE_STORE` | Parquet copy of the above made by `pyro-icon convert`, on scratch (§8) |

## 1. Summary

| Dataset | Model / product | Period | Time resolution | Members | Relevance for step 1 |
|---|---|---|---|---|---|
| **PD 2024–25 PPE** | ECHAM6.3-HAM2.3 (M7), nudged to ERA5 | 2024-08-01 → 2025-07-31 | **3-hourly** (`hifreq`), daily for some, monthly for the rest | Control + 250 | **Primary model data.** It only covers the Aug–Sep part of the 2024 fire season. |
| PD PPE, ensemble-stacked | same, post-processed | same | 3-hourly / daily / monthly | 251 in one file | The fastest route to event subsets |
| **MM PPE 2025** | ECHAM6-HAM (label `MMPPE`), AeroCom-format output | 2025-01-01 → 2025-12-31 | **6-hourly** and monthly | ~300 (+ tests) | 2025 fire season only; has 6-hourly fluxes and `ec355aer` profiles |
| **ICON-HAM PD PPE 2024–25** | ICON-HAM (R02B04, 20480 cells) | 15 monthly means, labels 2024-06-01 → 2025-08-01 | **monthly** | 392 (IDs 0–400, 9 missing) | Context only (not 3-hourly). Source folder is temporary; converted to parquet (§8) |
| PI counterparts | same models, 1850 emissions | same | same | same | Forcing baselines only |
| Monthly extracts (AeroCom_Error) | PD 2024 PPE | 2024-08 → 2025-07 | monthly | 251 | Monthly budgets and lifetime. **Scratch purge risk** (see §6) |
| AeroCom Phase III (local) | 7 models | 1850 / 2010 | monthly | — | **No 2024–25 and no 3-hourly data** |
| SPEXone RemoTAP L2 extracts | PACE SPEXone, land + ocean, v4.0 | 2024-06 → 2025-12 | per pixel | — | Ready for collocation (AOT 340–865 nm, SSA, uncertainties) |
| EarthCARE ATLID L3 | 2° daily mean extinction | 2024-08-01 → 2025-07-31 | daily | — | Profiles. Data before the public L2 release of 11 Mar 2025 need care |
| AERONET | V3 AOD, SSA (L1.5/L2.0 inversions) | multi-year | daily / monthly | — | Ground truth at the sites plumes reach |
| VIIRS (DB / DT / NOAA EPS), OMPS-LP | — | — | — | — | **Not on Snellius yet**; download in step 1c |

No TROPOMI aerosol product is used anywhere in this project.

## 2. PD 2024–25 PPE (primary)

- **Model setup.** ECHAM6.3-HAM2.3 with M7 microphysics on the T63 Gaussian grid (192 × 96, about 1.9°) with 47 hybrid sigma-pressure levels. Vorticity, divergence and surface pressure are nudged to ERA5 from 2024-04-01 to 2025-08-01, with temperature nudging off. Output starts on 2024-08-01; the spin-up months were removed. Because the winds follow ERA5, plume transport in the model can be compared directly with observed transport.
- **Fire emissions.** Daily GFAS (CAMS) wildfire emissions enter through HAM's `fire` emission type. Each member scales them with its own `SCALE_EMI_BB_{BC,OC,SO2}` values, drawn from a 28-parameter design in the PPE parameter table. HAM's standard fire profile only puts smoke into the lower troposphere, with no pyroCb or UTLS injection, so this PPE is the "pyroCb omitted" baseline. The exact vertical profile still needs to be checked in the HAM emission code.
- **Members.** Control plus `PPE_ENS_1`…`PPE_ENS_250` are all present. Every member has a complete 3-hourly file: 2920 steps from 2024-08-01 00 UTC to 2025-07-31 21 UTC, about 26 GB per member and 6.5 TB in total. With the monthly streams, each member directory holds about 32 GB, or about 8 TB for the ensemble.
- **Stacked files** (`PPE_STACKED_ROOT`). Arrays are `(ensemble=251, time=2920, lat=96, lon=192)` float32, stored contiguous and uncompressed, about 54 GB per variable. Each (member, step) slice is one contiguous 72 KB block, so cutting out a time window for all members is cheap.

### Variables against comparison needs

| Need | Model variable | Stream | Resolution | Status |
|---|---|---|---|---|
| AOD 550 nm | `TAU_2D_550nm` | hifreq, stacked | 3 h | ✅ |
| Ångström exponent | `ANG_440nm_670nm`, `ANG_550nm_865nm` | hifreq (440–670 also stacked) | 3 h | ✅ (fixed wavelength pairs) |
| Multi-wavelength AOD | `TAU_2D_{355,440,670,865}nm` | `*_rad_monthly` | monthly | ⚠️ no 3-hourly AOD outside 550 nm |
| Absorption AOD, SSA 550 nm | `ABS_2D_550nm`, `SSA_2D_550nm` (stacked) | hifreq | 3 h | ✅ (SSA = 1 − ABS/TAU) |
| SSA at other wavelengths | `OMEGA_2D_MODE_*` per mode | `*_rad_monthly` | monthly | ⚠️ |
| Mode AOD (KS, AS, CS, KI, AI, CI) | `TAU_2D_MODE_*_550nm` | hifreq, stacked | 3 h | ✅ fine/coarse split |
| Component AOD (BC, OC, SO4, SS, WAT) | `TAU_COMP_*_550nm_rad` | per member, stacked | daily | ✅ (DU monthly only) |
| Extinction profile | `TAU_3D_355nm` (layer optical depth, 47 levels) | hifreq | 3 h | ✅ at 355 nm only, which matches ATLID. Layer heights are not in hifreq ⚠️ |
| Layer heights | `geom1`, `grheightm1` | vphysc | monthly | ⚠️ for 3 h, rebuild from `aps` + hybrid coefficients + ERA5 temperature |
| Low-level AOD fraction | `TAU_355nm_frac_below2km` | per member, stacked | 3 h / daily / monthly | ✅ |
| Burdens BC, OC, SO4, SS, DU, water | `burden_*` | hifreq | 3 h | ✅ |
| Emissions (incl. `emi_{BC,OC,SO2}_tfire`) | `emi_*` | emi | monthly | ⚠️ for sub-monthly, use GFAS daily input × member scale factor (to be checked against the monthly diagnostic) |
| Wet / dry deposition | — | — | — | ❌ not written by this PPE (marked `missing` in the extract provenance), so lifetime has to come from burden/emission or from plume burden decay |
| Clouds (screening) | `aclcov`, `lcc`, `iccl` (3D), `xlvi`, CDNC, `REFFL_CT` | hifreq | 3 h | ✅ |
| Surface pressure | `aps` | hifreq | 3 h | ✅ |
| Winds / temperature | `svo`, `sd`, `st`, `q` | echam | monthly | ⚠️ use ERA5 for trajectories (the model is nudged to it) |
| TOA / surface fluxes, with and without aerosol | forcing stream, `TOA_*_flux_*` | per member, stacked | monthly | ❌ no sub-monthly fluxes, so event-scale forcing needs offline radiative transfer or a rerun |

## 3. MM PPE 2025

- Output is in AeroCom naming (`aerocom4_ECHAM6-HAM_MMPPE-<member>_<var>_<vertical>_2025_<freq>.nc`), about 6.9 GB per member, on the T63 grid. Time stamps run from 2025-01-01 05:52 to 2025-12-31 23:52 UTC.
- **6-hourly variables:** `od550aer` (also `_fine`, `_cs`, `_ci`), `abs550aer`, `angstrm440_670`, `angstrm550_865`, `ssa440aer`, `ec355aer` on 8 fixed heights (20 m to 10 km), `load{bc,oa,so4,ss,du,aerh2o}`, `clt`, `lcc`, `icc`, `cod`, `lwp`, `pr`, and TOA fluxes with and without aerosol (`rsut`, `rsutcs`, `rsutaf`, `rsutcsaf`, `rlut*`). That makes event-scale forcing possible for 2025.
- **Monthly variables:** emissions (`emibc`, `emioa`, …), mass mixing ratios on model levels, `zfull`/`zhalf`, meteorology. No deposition fields.
- Stacked 6-hourly ensemble files exist for a subset (`od550aer*`, `angstrm440_670`, `aerosol_index`).
- The output covers none of the 2024 events; it becomes useful once a 2025 boreal event list exists.

## 4. AeroCom

- The local copy has Phase III `AP3-CTRL*` monthly files for 7 models, for 1850 and 2010 only (ECHAM6.3-HAM2.3, ECHAM6-HAM2, ECHAM6.3-SALSA2.0, NorESM2, CAM5.3-Oslo, CAM5-ATRAS, TM5). No 2024–25 or 3-hourly AeroCom output was found on Snellius, and AeroCom_Error's `AP3_processed_3hourly` folder is empty.
- **Go/no-go (step 0):** someone has to list which models have 3-hourly output for Aug 2024–Jul 2025 on the MET Norway AeroCom users' server. That needs Jessie's own account and was **not** checked here.

## 5. Event-table overlap with the data

The 2024 boreal list covers onsets from May to September 2024 in Alaska, Canada and Northern Asia. Measured against the periods above (`pyro-events coverage`, with windows from onset − 1 d to onset + 10 d and kept events only):

- **PD PPE (3-hourly):** only the August–September events, about one fifth of the kept list, have onset and full window inside the model period. No Alaska events are covered; Alaska's 2024 events were in June.
- **MM PPE 2025:** none.
- **SPEXone extracts:** every event in every region has SPEXone pixels within 500 km in its window, except the earliest events whose window lies entirely in May 2024 (no May extract). Details and the 3-hourly PPE sampling at those pixels: `pyro-match`, pipeline plan §1e.
- **Public ATLID L2:** none (it starts 11 Mar 2025).

Options, to be decided with the fire side:
1. Ask for the **2025 boreal events**. May–Jul 2025 is covered by both PPEs, and Aug–Sep 2025 by the MM PPE.
2. Rerun the spin-up months (May–Jul 2024) with 3-hourly output, either Control-only or a member subset, to cover the June–July 2024 peak.
3. Treat 2024 as a small pilot for the August–September events only.

## 6. Risks and gaps flagged

1. **Scratch purge.** Snellius deletes `/scratch-shared` files that have not been modified for 14 days, without notice or backup. The monthly ensemble extracts under `PPE_EXTRACT_ROOT/pd2024` and the ICON-HAM parquet store live there. `scripts/scratch_keepalive.sh start` runs a self-resubmitting 1-core `staging` job that refreshes modification times every 12 days (cron/scrontab are disabled on Snellius). This is a stop-gap: copy anything that matters to Google Drive / project space with `pyro-sync`, and regenerate the extracts with `AeroCom_Error/py/ppe_extract_monthly.py` if needed.
2. **AeroCom 3-hourly 2024–25** is unverified (§4).
3. **Sub-monthly emissions, deposition and fluxes** are missing from the PD PPE. This limits the lifetime and forcing terms at event scale.
4. **Vertical information** exists only as layer AOD at 355 nm, without 3-hourly layer heights.
5. **Ångström pairs are fixed** at 440–670 and 550–865. VIIRS uses other pairs, so compare pyroCb − control differences or recompute satellite AE where the spectral data allow it.
6. **VIIRS and OMPS-LP** still have to be downloaded.

## 7. Volume estimates for subsets

T63 has 16 Gaussian rows between 45° and 75° N and 22 rows between 40° and 80° N.

| Subset (all 251 members, float32) | Size |
|---|---|
| One 2D variable, 45–75° N band, full year at 3 h | ≈ 9 GB |
| 12 2D variables, same | ≈ 110 GB |
| `TAU_3D_355nm` (47 levels), same | ≈ 420 GB |
| One event window (11 d, 40–80° N band), 12 2D variables | ≈ 4.5 GB |
| Same, plus the 47-level `TAU_3D_355nm` | ≈ 22 GB |
| Union of all 2024 event windows inside the PPE period (≈ 6 weeks, 40–80° N), 12 2D variables | ≈ 18 GB |
| Same, plus the 47-level profile | ≈ 90 GB |

Event windows overlap in time, so subset the time union once, not per event. For the profile, a tighter longitude sector per event cuts the 3D volume further.

## 8. ICON-HAM PD PPE 2024–25 (monthly)

- **Source** (`ICON_PPE_SOURCE_ROOT`, a temporary shared folder that will be removed): one directory per variable, one file per member, `ens_<NNN>_PD_<var>.nc`, made with `cdo mergetime` from the `*_tm_ml` monthly mean output. 22 GB, 24 variables × 392 members.
- **Grid / time:** ICON R02B04 (`icon_grid_0005_R02B04_G`, 20480 triangular cells, ~160 km), cell centres and vertices in radians. 15 time steps stamped `day as %Y%m%d` from 20240601 to 20250801. ICON stamps mean output at the **end** of the averaging interval by default, so the 2024-06-01 value is probably the May 2024 mean; confirm with the data provider before month-specific use.
- **Variables:** AOD and Ångström: `TAU_2D_550nm`, `ABS_2D_550nm`, `ANG_440nm_670nm`, `ANG_550nm_865nm`, `TAU_COMP_{BC,DU,OC,SO4,SS,WAT}_550nm`. Dry deposition: `ddep_{bc,du,oc,so2,so4,ss}`. Emissions: `emi_{bc,dms,du,oc,so2,so4,ss}`. Precipitation: `pr`. Burdens (`burden_*`) were being retrieved from the archive at the time of writing; `pyro-icon convert` picks up any new variable directory on a rerun.
- **Members:** 392 of IDs 000–400. Missing: 013, 040, 055, 063, 069, 116, 117, 260, 375 (the same for every variable).
- **Quality flags found during conversion** (values kept as in the source; listed in `metadata.json` → `known_anomalies`): member 219 at label 2024-06-01 has dust-dominated AOD > 10 (max ≈ 72) in 58 cells; member 213 at label 2025-08-01 has dry dust deposition up to ≈ 1e4 kg m⁻² s⁻¹ in a few cells near 7° N 27° E. Small negative values occur in `ddep_*` and AOD (numerical noise). Exclude or clip these before computing ensemble statistics.
- **Time labels, first check:** over western Canada, AOD and `emi_oc` peak at labels 2024-08-01 and 2024-09-01, consistent with end-of-interval stamps (July and August means).
- **Relevance:** monthly means cannot be matched to overpasses (step 1 uses 3-hourly output only). Useful for monthly budgets, deposition-based lifetime (ECHAM-HAM PD PPE has no deposition output) and a second-model PPE spread.
- **Parquet store** (`ICON_PPE_STORE`, `pyro-icon convert` / `scripts/slurm/icon_ppe_convert.sbatch`): `values/time_label=YYYY-MM-DD/part-0.parquet`, one wide table per time step with `member, cell, lat, lon` and all variables as float32 columns, rows sorted by latitude, zstd + byte-stream-split; plus `grid.parquet` (cell centres and vertices in degrees), `members.parquet` (availability) and `metadata.json` (units, long names, float32 rounding error per variable, time-label note). `pyro-icon verify` compares random (variable, member) slices bit-for-bit with the float32-cast source. Query with polars:

  ```python
  from pyro.store import scan

  lf = scan(f"{ICON_PPE_STORE}/values")
  lf.filter(pl.col("lat").is_between(50, 70)).group_by("time_label", "member").agg(
      pl.col("emi_oc").mean()
  )
  ```
