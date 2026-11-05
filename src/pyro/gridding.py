"""Gridding of satellite pixels and regional aggregates, shared by SPEXone and VIIRS.

A polars port of the AeroCom_Error building blocks:

* ``grid_regular`` follows ``SPEXone_regrid_model.regrid_spexone``: bins of
  ``res_deg`` with centres ``floor(x / res) * res + res / 2``, time rounded to
  ``time_freq`` (default 3 h), centre = mean or median, spread = std or half-IQR,
  uncertainty = mean of the pixel uncertainties (fully correlated, no sqrt(N)),
  bins with fewer than ``min_points`` pixels dropped.
* ``grid_model`` puts the same aggregation on a PPE source's own grid and time
  steps (nearest 3-hourly step and model cell through ``pyro.ppe.locate``), which
  gives the super-obs that are sampled against the ensemble.
* ``regional_means`` follows ``regional_aggregates.weights_from_defs`` /
  ``regional_means``: cos(latitude)-weighted means over region boxes, skipping
  missing values.
* ``pixels_near_events`` keeps the pixels a satellite swath put within a radius of
  each event's fire position inside its window, for any pixel product.
"""

from __future__ import annotations

import math

import polars as pl

from pyro import ppe

EARTH_RADIUS_KM = 6371.0


def _wrap180(col: str = "lon") -> pl.Expr:
    return ((pl.col(col) + 180.0) % 360.0) - 180.0


def _aggregations(
    values: dict[str, str],
    agg: str,
    spread: str,
    uncertainty: dict[str, str] | None,
) -> list[pl.Expr]:
    if agg not in ("mean", "median"):
        raise ValueError(f"agg must be mean or median, not {agg!r}")
    if spread not in ("std", "iqr"):
        raise ValueError(f"spread must be std or iqr, not {spread!r}")
    out = []
    for name, col in values.items():
        c = pl.col(col).filter(pl.col(col).is_finite())
        out.append((c.mean() if agg == "mean" else c.median()).alias(name))
        if spread == "std":
            out.append(c.std().alias(f"{name}_spread"))
        else:
            out.append(
                (0.5 * (c.quantile(0.75, "linear") - c.quantile(0.25, "linear"))).alias(
                    f"{name}_spread"
                )
            )
    for name, col in (uncertainty or {}).items():
        out.append(pl.col(col).filter(pl.col(col).is_finite()).mean().alias(f"{name}_unc"))
    return out


def _round_time(col: str, freq: str | None) -> pl.Expr:
    if not freq:
        return pl.col(col)
    if freq in ("1mo", "MS", "M"):
        return pl.col(col).dt.truncate("1mo")
    return pl.col(col).dt.round(freq)


def grid_regular(
    pixels: pl.DataFrame,
    values: dict[str, str],
    *,
    res_deg: float = 1.0,
    time_freq: str | None = "3h",
    keys: tuple[str, ...] = (),
    agg: str = "mean",
    spread: str = "std",
    uncertainty: dict[str, str] | None = None,
    min_points: int = 2,
    time_col: str = "time",
) -> pl.DataFrame:
    """Bins (``lon_bin``, ``lat_bin``, ``date``) of ``res_deg`` degrees.

    ``values`` maps output names to pixel columns, ``uncertainty`` the same for
    per-pixel uncertainties. Output per bin: ``n``, each value, ``<value>_spread``,
    ``<name>_unc``.
    """
    if pixels.is_empty():
        return pl.DataFrame()
    df = pixels.with_columns(
        ((_wrap180() / res_deg).floor() * res_deg + res_deg / 2).cast(pl.Float32).alias("lon_bin"),
        ((pl.col("lat") / res_deg).floor() * res_deg + res_deg / 2)
        .cast(pl.Float32)
        .alias("lat_bin"),
        _round_time(time_col, time_freq).alias("date"),
    )
    group = [*keys, "lon_bin", "lat_bin", "date"]
    return (
        df.group_by(group)
        .agg(pl.len().alias("n"), *_aggregations(values, agg, spread, uncertainty))
        .filter(pl.col("n") >= min_points)
        .sort(group)
    )


def grid_model(
    pixels: pl.DataFrame,
    src: ppe.Source,
    values: dict[str, str],
    *,
    keys: tuple[str, ...] = (),
    agg: str = "mean",
    spread: str = "std",
    uncertainty: dict[str, str] | None = None,
    min_points: int = 1,
    time_col: str = "time",
) -> pl.DataFrame:
    """Super-obs per (``keys``, model step ``t_idx``, model ``cell``) of ``src``.

    Pixels farther than half a model step from every step are dropped. Columns:
    ``keys``, ``t_idx``, ``cell``, ``model_time``, ``model_lat``, ``model_lon``,
    ``n_pix``, ``obs_time`` (mean pixel time), the aggregated values, and
    ``dataset``/``ppe_source``/``resolution`` of the source.
    """
    if pixels.is_empty():
        return pl.DataFrame()
    pts = pixels.select(
        pl.int_range(pl.len(), dtype=pl.Int64).alias("point_id"),
        pl.col(time_col).alias("time"),
        "lat",
        "lon",
    )
    loc = ppe.locate(src, pts).drop("point_id", "dt_hours")
    m = pl.concat([pixels, loc], how="horizontal").filter(pl.col("t_idx") >= 0)
    group = [*keys, "t_idx", "cell"]
    return (
        m.group_by(group)
        .agg(
            pl.col("model_time", "model_lat", "model_lon").first(),
            pl.len().alias("n_pix"),
            pl.col(time_col).mean().alias("obs_time"),
            *_aggregations(values, agg, spread, uncertainty),
        )
        .filter(pl.col("n_pix") >= min_points)
        .with_columns(
            pl.lit(src.key).alias("dataset"),
            pl.lit(src.ppe_source).alias("ppe_source"),
            pl.lit(src.resolution).alias("resolution"),
        )
        .sort(group)
    )


def assign_boxes(
    df: pl.DataFrame,
    regions: pl.DataFrame,
    *,
    lat_col: str = "lat",
    lon_col: str = "lon",
) -> pl.DataFrame:
    """Add ``region`` (first box containing the point); drop points outside all boxes.

    ``regions`` has ``region, lat_min, lat_max, lon_min, lon_max`` (lon in
    [-180, 180), lon_max exclusive), as returned by ``pyro.regional.load_regions``.
    """
    lon = _wrap180(lon_col)
    expr = pl.lit(None, dtype=pl.String)
    for r in regions.iter_rows(named=True):
        inside = pl.col(lat_col).is_between(
            r["lat_min"], r["lat_max"], closed="left"
        ) & lon.is_between(r["lon_min"], r["lon_max"], closed="left")
        expr = pl.when(inside).then(pl.lit(r["region"])).otherwise(expr)
    return df.with_columns(expr.alias("region")).filter(pl.col("region").is_not_null())


def regional_means(
    binned: pl.DataFrame,
    regions: pl.DataFrame,
    values: list[str],
    *,
    lat_col: str = "lat_bin",
    lon_col: str = "lon_bin",
    by: tuple[str, ...] = (),
    count_col: str | None = "n",
) -> pl.DataFrame:
    """cos(lat)-weighted mean of each value per (region, *by), missing values skipped.

    Equal-angle bins have area proportional to cos(lat), so this matches the
    AeroCom_Error weights (cos(lat) x cell size) for regular bins and is the same
    weighting for model cells of one Gaussian grid row spacing.
    """
    if binned.is_empty():
        return pl.DataFrame()
    df = assign_boxes(binned, regions, lat_col=lat_col, lon_col=lon_col).with_columns(
        pl.col(lat_col).cast(pl.Float64).radians().cos().alias("_w")
    )
    aggs = [pl.len().alias("n_bins")]
    if count_col and count_col in df.columns:
        aggs.append(pl.col(count_col).sum().alias("n_pix"))
    for v in values:
        ok = pl.col(v).is_finite()
        aggs.append(
            ((pl.col(v) * pl.col("_w")).filter(ok).sum() / pl.col("_w").filter(ok).sum()).alias(v)
        )
    keys = ["region", *by]
    return df.group_by(keys).agg(aggs).sort(keys)


def _dist_km(lat0: float, lon0: float) -> pl.Expr:
    p1, p2 = math.radians(lat0), pl.col("lat").radians()
    dlmb = (pl.col("lon") - lon0).radians()
    a = ((p2 - p1) / 2).sin() ** 2 + math.cos(p1) * p2.cos() * (dlmb / 2).sin() ** 2
    return 2 * EARTH_RADIUS_KM * a.sqrt().arcsin()


def pixels_near_events(
    events: pl.DataFrame,
    pixels: pl.DataFrame,
    radius_km: float,
    *,
    time_col: str = "time",
    keep: tuple[str, ...] = ("pcb_id", "region", "kept", "onset_utc", "lat_c", "lon_c"),
) -> pl.DataFrame:
    """One row per (event, pixel) within ``radius_km`` of ``lon_c``/``lat_c`` and inside
    ``window_start``..``window_end``; adds ``dist_km`` and ``dt_hours`` (since onset)."""
    out = []
    dlat = radius_km / 111.0
    for ev in events.iter_rows(named=True):
        coslat = max(math.cos(math.radians(ev["lat_c"])), 0.05)
        dlon = min(radius_km / (111.0 * coslat), 180.0)
        wrapped = ((pl.col("lon") - ev["lon_c"] + 180.0) % 360.0) - 180.0
        sub = pixels.filter(
            pl.col(time_col).is_between(ev["window_start"], ev["window_end"])
            & (pl.col("lat") - ev["lat_c"]).abs().le(dlat)
            & wrapped.abs().le(dlon)
        )
        if sub.is_empty():
            continue
        sub = sub.with_columns(_dist_km(ev["lat_c"], ev["lon_c"]).alias("dist_km")).filter(
            pl.col("dist_km") <= radius_km
        )
        if not sub.is_empty():
            out.append(sub.with_columns([pl.lit(ev[k]).alias(k) for k in keep if k in ev]))
    if not out:
        return pl.DataFrame()
    return pl.concat(out, how="vertical").with_columns(
        ((pl.col(time_col) - pl.col("onset_utc")).dt.total_seconds() / 3600.0).alias("dt_hours")
    )
