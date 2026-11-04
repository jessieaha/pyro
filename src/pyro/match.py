"""Match pyroCb events with PACE SPEXone pixels and sample the 3-hourly PPE there.

``spexone``: for every event in the table (all regions, kept or not) select the
SPEXone RemoTAP L2 pixels (land + ocean) inside the event window and within
``--max-km`` of the matched fire position, and summarise per event.

``model``: for each PPE source (``--ppe``, read through ``pyro.ppe``) average the
matched pixels into super-obs per (event, model time step, model cell) and sample
every member there, in the common PPE schema. Only 3-hourly sources are used
unless ``--allow-coarse`` is given.

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

import polars as pl

from pyro import ppe
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

# Canonical PPE variables (pyro.ppe) sampled at the super-obs.
MODEL_VARIABLES = ["od550aer", "abs550aer", "ssa550aer", "ae440_670", "ae550_865"]
DEFAULT_PPE = ["ybhatti_pd_3h"]


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


def superobs(matches: pl.DataFrame, src: ppe.Source, max_km: float) -> pl.DataFrame:
    """Average pixels per (event, model time step, model cell) of ``src``."""
    m = matches.filter(pl.col("dist_km") <= max_km)
    if m.is_empty():
        return pl.DataFrame()
    pts = m.select(
        pl.int_range(pl.len(), dtype=pl.Int64).alias("point_id"),
        pl.col("date").alias("time"),
        "lat",
        "lon",
    )
    loc = ppe.locate(src, pts).drop("point_id", "dt_hours")
    m = pl.concat([m, loc], how="horizontal").filter(pl.col("t_idx") >= 0)
    ae = -(pl.col("aot_440nm") / pl.col("aot_670nm")).log() / math.log(440 / 670)
    return (
        m.group_by("pcb_id", "region", "kept", "t_idx", "cell")
        .agg(
            pl.col("model_time", "model_lat", "model_lon").first(),
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
            pl.lit(src.key).alias("dataset"),
            pl.lit(src.ppe_source).alias("ppe_source"),
            pl.lit(src.resolution).alias("resolution"),
        )
        .sort("pcb_id", "t_idx", "cell")
    )


def sample_model(
    so: pl.DataFrame, src: ppe.Source, variables: list[str] | None = None
) -> pl.DataFrame:
    """All members of ``src`` at the super-obs (common PPE schema plus ``pcb_id``)."""
    if so.is_empty():
        return pl.DataFrame()
    want = [
        v
        for v in variables or MODEL_VARIABLES
        if set(ppe.DERIVED.get(v, (v,))) <= set(src.available())
    ]
    pts = so.select(
        pl.int_range(pl.len(), dtype=pl.Int64).alias("point_id"),
        pl.col("obs_time").alias("time"),
        pl.col("model_lat").alias("lat"),
        pl.col("model_lon").alias("lon"),
    )
    df = ppe.sample(src, pts, want)
    keys = so.select(
        pl.int_range(pl.len(), dtype=pl.Int64).alias("point_id"), "pcb_id", "t_idx", "cell"
    )
    check = df.select("point_id", "t_idx", "cell").unique().join(keys, on="point_id")
    if (
        not (check["t_idx"] == check["t_idx_right"]).all()
        or not (check["cell"] == check["cell_right"]).all()
    ):
        raise RuntimeError("super-obs moved to another model step or cell when resampled")
    return df.join(keys.select("point_id", "pcb_id"), on="point_id").select("pcb_id", *df.columns)


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
    p.add_argument("--model", action="store_true", help="also sample the PPE (see --ppe)")
    p.add_argument("--model-km", type=float, default=500.0)
    p.add_argument(
        "--ppe",
        action="append",
        default=None,
        help=f"PPE source key from config/ppe_sources.toml (repeatable; default {DEFAULT_PPE})",
    )
    p.add_argument("--ppe-config", type=Path, default=None)
    p.add_argument(
        "--allow-coarse",
        action="store_true",
        help="also match sources coarser than 3-hourly (6-hourly, monthly); results are "
        "flagged by the 'resolution' column",
    )
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
        cfg = ppe.load_config(a.ppe_config)
        for key in a.ppe or DEFAULT_PPE:
            if key not in cfg:
                p.error(f"unknown PPE source {key!r}; configured: {sorted(cfg)}")
            if cfg[key].get("resolution") != "3h" and not a.allow_coarse:
                p.error(
                    f"{key} is {cfg[key].get('resolution')}; satellite matching uses "
                    "3-hourly output only (override with --allow-coarse)"
                )
            src = ppe.open_source(key, a.ppe_config)
            so = superobs(matches, src, a.model_km) if not matches.is_empty() else pl.DataFrame()
            print(f"{key}: {len(so)} super-obs within {a.model_km:.0f} km", file=sys.stderr)
            if so.is_empty():
                continue
            so.write_parquet(out / f"superobs_{key}.parquet", compression="zstd")
            sample_model(so, src).write_parquet(out / f"model_{key}.parquet", compression="zstd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
