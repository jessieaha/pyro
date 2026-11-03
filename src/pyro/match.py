"""Match pyroCb events with PACE SPEXone pixels and sample the 3-hourly PPE there.

``spexone``: for every event in the table (all regions, kept or not) select the
SPEXone RemoTAP L2 pixels (land + ocean) inside the event window and within
``--max-km`` of the matched fire position, and summarise per event.

``model``: average the matched pixels into super-obs on the ECHAM T63 grid at the
nearest 3-hourly step, and sample every PPE member from the ensemble-stacked
3-hourly (``*_hifreq_PPE.nc``) files. Only 3-hourly model output is used.

Outputs contain event identifiers and are restricted data (written under
``$PYRO_DATA_ROOT/derived``, never committed).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path

import numpy as np
import polars as pl

from pyro.events import PERIODS, add_windows, load_events

EARTH_RADIUS_KM = 6371.0
RADII_KM = (150, 500, 1000)
# AOD threshold used to flag smoke-laden pixels (screening only, not attribution).
SMOKY_AOD = 0.5
SPEX_COLUMNS = (
    "lat",
    "lon",
    "date",
    "aot_440nm",
    "aot_550nm",
    "aot_670nm",
    "aot_865nm",
    "aot_uncertainty_550nm",
    "ssa_440nm",
    "ssa_550nm",
    "ssa_uncertainty_550nm",
)
SPEX_RE = re.compile(r"PACE_SPEXONE_L2_AER_RTAP(LAND|OCEAN)_(\d{4})_(\d{2})_v([\d.]+)\.parquet$")

# Stacked 3-hourly PPE files: file stem -> (netCDF variable, output column).
MODEL_FIELDS = {
    "TAU_2D_550nm_hifreq_PPE": ("TAU_2D_550nm", "m_aod550"),
    "ABS_2D_550nm_hifreq_PPE": ("ABS_2D_550nm", "m_aaod550"),
    "SSA_2D_550nm_hifreq_PPE": ("__xarray_dataarray_variable__", "m_ssa550"),
    "ANG_440nm_670nm_hifreq_PPE": ("ANG_440nm_670nm", "m_ae440_670"),
}
MODEL_STEP_DAYS = 0.125


def spexone_files(root: Path) -> pl.DataFrame:
    rows = []
    for f in sorted(root.glob("*.parquet")):
        m = SPEX_RE.search(f.name)
        if m:
            surface, y, mo, ver = m.groups()
            rows.append(
                {"path": str(f), "surface": surface.lower(), "month": f"{y}-{mo}", "version": ver}
            )
    if not rows:
        raise FileNotFoundError(f"no SPEXone extract files under {root}")
    return pl.DataFrame(rows)


def _months(start, end) -> set[str]:
    out, y, m = set(), start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.add(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def load_spexone(root: Path, events: pl.DataFrame, max_km: float) -> pl.DataFrame:
    """Read only the columns, months and latitude band needed for ``events``."""
    files = spexone_files(root)
    months: set[str] = set()
    for s, e in events.select("window_start", "window_end").iter_rows():
        months |= _months(s, e)
    files = files.filter(pl.col("month").is_in(sorted(months)))
    if files.is_empty():
        return pl.DataFrame()
    dlat = max_km / 111.0
    lat_lo = events["lat_c"].min() - dlat
    lat_hi = events["lat_c"].max() + dlat
    t_lo, t_hi = events["window_start"].min(), events["window_end"].max()
    frames = []
    for path, surface in files.select("path", "surface").iter_rows():
        lf = (
            pl.scan_parquet(path)
            .select(SPEX_COLUMNS)
            .filter(
                pl.col("lat").is_between(lat_lo, lat_hi)
                & pl.col("date").is_between(t_lo, t_hi)
                & pl.col("aot_550nm").is_not_null()
                & pl.col("aot_550nm").is_not_nan()
            )
            .with_columns(pl.lit(surface).alias("surface"))
        )
        frames.append(lf)
    return pl.concat(pl.collect_all(frames), how="vertical")


def _dist_km(lat0: float, lon0: float) -> pl.Expr:
    p1, p2 = math.radians(lat0), pl.col("lat").radians()
    dphi = p2 - p1
    dlmb = (pl.col("lon") - lon0).radians()
    a = (dphi / 2).sin() ** 2 + math.cos(p1) * p2.cos() * (dlmb / 2).sin() ** 2
    return (2 * EARTH_RADIUS_KM * a.sqrt().arcsin()).alias("dist_km")


def match_events(events: pl.DataFrame, spex: pl.DataFrame, max_km: float) -> pl.DataFrame:
    """One row per (event, SPEXone pixel) within the window and ``max_km``."""
    keep = ["pcb_id", "region", "kept", "onset_utc", "lat_c", "lon_c"]
    out = []
    dlat = max_km / 111.0
    for ev in events.iter_rows(named=True):
        coslat = max(math.cos(math.radians(ev["lat_c"])), 0.05)
        dlon = min(max_km / (111.0 * coslat), 180.0)
        wrapped = ((pl.col("lon") - ev["lon_c"] + 180.0) % 360.0) - 180.0
        sub = spex.filter(
            pl.col("date").is_between(ev["window_start"], ev["window_end"])
            & (pl.col("lat") - ev["lat_c"]).abs().le(dlat)
            & wrapped.abs().le(dlon)
        )
        if sub.is_empty():
            continue
        sub = sub.with_columns(_dist_km(ev["lat_c"], ev["lon_c"])).filter(
            pl.col("dist_km") <= max_km
        )
        if sub.is_empty():
            continue
        out.append(sub.with_columns([pl.lit(ev[k]).alias(k) for k in keep]))
    if not out:
        return pl.DataFrame()
    m = pl.concat(out, how="vertical")
    return m.with_columns(
        ((pl.col("date") - pl.col("onset_utc")).dt.total_seconds() / 3600.0).alias("dt_hours"),
        pl.col("date").dt.date().alias("overpass_day"),
    ).select(
        *keep,
        "surface",
        "date",
        "dt_hours",
        "overpass_day",
        "dist_km",
        "lat",
        "lon",
        *SPEX_COLUMNS[3:],
    )


def summarize(events: pl.DataFrame, matches: pl.DataFrame) -> pl.DataFrame:
    """Per-event match statistics; events without any pixel are kept with zeros."""
    base = events.select("pcb_id", "region", "kept", "onset_utc", "lat_c", "lon_c")
    if matches.is_empty():
        return base.with_columns(pl.lit(0).alias(f"n_pix_{r}km") for r in RADII_KM)
    aggs = []
    for r in RADII_KM:
        within = pl.col("dist_km") <= r
        aggs += [
            within.sum().alias(f"n_pix_{r}km"),
            pl.col("overpass_day").filter(within).n_unique().alias(f"n_days_{r}km"),
        ]
    near = pl.col("dist_km") <= 500
    smoky = pl.col("aot_550nm") >= SMOKY_AOD
    early = (pl.col("dist_km") <= 150) & pl.col("dt_hours").is_between(-24, 48)
    aggs += [
        early.sum().alias("n_pix_150km_m1d_p2d"),
        (early & smoky).sum().alias("n_pix_smoky_150km_m1d_p2d"),
        pl.col("dt_hours").filter(near).min().alias("first_dt_hours_500km"),
        pl.col("dist_km").min().alias("min_dist_km"),
        pl.col("aot_550nm").filter(near).median().alias("aot550_median_500km"),
        pl.col("aot_550nm").filter(near).max().alias("aot550_max_500km"),
        (near & smoky).sum().alias("n_pix_smoky_500km"),
        pl.col("ssa_550nm").filter(near & smoky).median().alias("ssa550_median_smoky_500km"),
    ]
    s = matches.group_by("pcb_id").agg(aggs)
    out = base.join(s, on="pcb_id", how="left")
    counts = [c for c in out.columns if c.startswith(("n_pix_", "n_days_"))]
    lo, hi = PERIODS["ppe_pd2024_3h"]
    return (
        out.with_columns(pl.col(counts).fill_null(0))
        .with_columns(
            pl.col("onset_utc")
            .is_between(
                pl.lit(lo).str.to_datetime(time_zone="UTC"),
                pl.lit(hi).str.to_datetime(time_zone="UTC"),
                closed="left",
            )
            .alias("onset_in_ppe_3h"),
            (pl.col("n_pix_500km") > 0).alias("matched_500km"),
            (pl.col("n_pix_smoky_500km") > 0).alias("smoky_500km"),
            (pl.col("n_pix_smoky_150km_m1d_p2d") > 0).alias("smoky_near_early"),
        )
        .sort("onset_utc")
    )


# --------------------------------------------------------------------------- model


def _model_axes(path: Path) -> dict:
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        units = ds["time"].units
        m = re.match(r"days since (\d{4}-\d{2}-\d{2})", units)
        if not m:
            raise ValueError(f"unexpected time units {units!r} in {path}")
        return {
            "time": np.asarray(ds["time"][:], dtype=np.float64),
            "t0": m.group(1),
            "lat": np.asarray(ds["lat"][:], dtype=np.float64),
            "lon": np.asarray(ds["lon"][:], dtype=np.float64),
            "ensemble": np.asarray(ds["ensemble"][:]),
        }


def superobs(matches: pl.DataFrame, axes: dict, max_km: float) -> pl.DataFrame:
    """Average pixels per (event, 3-hourly step, T63 cell)."""
    lat, lon, tm = axes["lat"], axes["lon"], axes["time"]
    epoch = pl.lit(axes["t0"]).str.to_datetime(time_zone="UTC")
    m = matches.filter(pl.col("dist_km") <= max_km).with_columns(
        ((pl.col("date") - epoch).dt.total_seconds() / 86400.0).alias("t_days")
    )
    t_idx = np.rint((m["t_days"].to_numpy() - tm[0]) / MODEL_STEP_DAYS).astype(np.int64)
    dlon = float(lon[1] - lon[0])
    i_lon = np.rint(((m["lon"].to_numpy() - lon[0]) % 360.0) / dlon).astype(np.int64) % lon.size
    i_lat = np.abs(m["lat"].to_numpy()[:, None] - lat[None, :]).argmin(axis=1)
    m = m.with_columns(
        pl.Series("t_idx", t_idx), pl.Series("lat_idx", i_lat), pl.Series("lon_idx", i_lon)
    ).filter(pl.col("t_idx").is_between(0, tm.size - 1))
    ae = -(pl.col("aot_440nm") / pl.col("aot_670nm")).log() / math.log(440 / 670)
    return (
        m.group_by("pcb_id", "region", "kept", "t_idx", "lat_idx", "lon_idx")
        .agg(
            pl.len().alias("n_pix"),
            pl.col("date").mean().alias("obs_time"),
            pl.col("dt_hours").mean(),
            pl.col("dist_km").mean(),
            pl.col("surface").mode().first(),
            pl.col("aot_550nm").mean().alias("o_aod550"),
            pl.col("aot_550nm").std().alias("o_aod550_std"),
            pl.col("aot_uncertainty_550nm").mean().alias("o_aod550_unc"),
            pl.col("ssa_550nm").mean().alias("o_ssa550"),
            ae.mean().alias("o_ae440_670"),
        )
        .with_columns(
            (pl.col("o_aod550") * (1 - pl.col("o_ssa550"))).alias("o_aaod550"),
            pl.col("t_idx")
            .map_elements(lambda i: float(tm[i]), return_dtype=pl.Float64)
            .alias("model_time_days"),
        )
        .sort("pcb_id", "t_idx", "lat_idx", "lon_idx")
    )


def sample_model(so: pl.DataFrame, stacked_root: Path, axes: dict) -> pl.DataFrame:
    """Sample each stacked 3-hourly field for every member at the super-obs points."""
    import netCDF4

    if so.is_empty():
        return pl.DataFrame()
    t = so["t_idx"].to_numpy()
    j = so["lat_idx"].to_numpy()
    i = so["lon_idx"].to_numpy()
    t0, t1 = int(t.min()), int(t.max()) + 1
    n_ens = axes["ensemble"].size
    cols: dict[str, np.ndarray] = {}
    for stem, (var, col) in MODEL_FIELDS.items():
        path = stacked_root / f"{stem}.nc"
        if not path.exists():
            print(f"missing {path}, skipping {col}", file=sys.stderr)
            continue
        vals = np.empty((n_ens, len(so)), dtype=np.float32)
        with netCDF4.Dataset(path) as ds:
            v = ds[var]
            v.set_auto_mask(False)
            for e in range(n_ens):
                slab = v[e, t0:t1]
                vals[e] = slab[t - t0, j, i]
        cols[col] = vals.reshape(-1)
        print(f"sampled {col}", file=sys.stderr, flush=True)
    rep = so.select("pcb_id", "t_idx", "lat_idx", "lon_idx")
    out = pl.concat([rep] * n_ens, how="vertical").with_columns(
        pl.Series("member", np.repeat(axes["ensemble"].astype(np.int32), len(so)))
    )
    return out.with_columns(pl.Series(k, v) for k, v in cols.items())


# --------------------------------------------------------------------------- CLI


def _derived(root: str | None, sub: str) -> Path:
    if not root:
        raise SystemExit("PYRO_DATA_ROOT (or --out) is required")
    return Path(root) / "derived" / sub


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pyro-match", description=__doc__.splitlines()[0])
    p.add_argument("events", type=Path, help="event table (CSV or parquet)")
    p.add_argument("--pre-days", type=float, default=1.0)
    p.add_argument("--post-days", type=float, default=10.0)
    p.add_argument("--max-km", type=float, default=float(max(RADII_KM)))
    p.add_argument("--spexone-root", type=Path, default=os.environ.get("SPEXONE_EXTRACT_ROOT"))
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--model", action="store_true", help="also sample the 3-hourly PPE")
    p.add_argument("--model-km", type=float, default=500.0)
    p.add_argument("--stacked-root", type=Path, default=os.environ.get("PPE_STACKED_ROOT"))
    a = p.parse_args(argv)
    if a.spexone_root is None:
        p.error("--spexone-root or SPEXONE_EXTRACT_ROOT is required")
    out = a.out or _derived(os.environ.get("PYRO_DATA_ROOT"), "spexone_event_match")
    out.mkdir(parents=True, exist_ok=True)

    events = add_windows(load_events(a.events), a.pre_days, a.post_days)
    spex = load_spexone(a.spexone_root, events, a.max_km)
    print(f"{len(events)} events, {len(spex)} SPEXone pixels in the event band", file=sys.stderr)
    matches = match_events(events, spex, a.max_km)
    summary = summarize(events, matches)
    if not matches.is_empty():
        matches.write_parquet(out / "pixels.parquet", compression="zstd")
    summary.write_parquet(out / "summary.parquet", compression="zstd")
    summary.write_csv(out / "summary.csv")

    by_region = (
        summary.group_by("region")
        .agg(
            pl.len().alias("events"),
            pl.col("kept").sum().alias("kept"),
            *[(pl.col(f"n_pix_{r}km") > 0).sum().alias(f"matched_{r}km") for r in RADII_KM],
            pl.col("smoky_500km").sum(),
            pl.col("smoky_near_early").sum(),
            pl.col("onset_in_ppe_3h").sum().alias("in_ppe_3h"),
            (pl.col("onset_in_ppe_3h") & pl.col("matched_500km"))
            .sum()
            .alias("ppe_3h_and_matched_500km"),
        )
        .sort("region")
    )
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200):
        print(by_region)

    if a.model:
        if a.stacked_root is None:
            p.error("--stacked-root or PPE_STACKED_ROOT is required with --model")
        axes = _model_axes(a.stacked_root / "TAU_2D_550nm_hifreq_PPE.nc")
        so = superobs(matches, axes, a.model_km) if not matches.is_empty() else pl.DataFrame()
        print(
            f"{len(so)} super-obs within {a.model_km:.0f} km in the 3-hourly period",
            file=sys.stderr,
        )
        if not so.is_empty():
            so.write_parquet(out / "superobs.parquet", compression="zstd")
            sample_model(so, a.stacked_root, axes).write_parquet(
                out / "model_3h_members.parquet", compression="zstd"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
