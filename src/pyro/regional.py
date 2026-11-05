"""Regional model-vs-satellite statistics: pyroCb-affected scenes against the regional average.

For each region that has pyroCb events (``config/regions.toml``) every SPEXone pixel
in the region box during the fire season is labelled

* ``pyrocb``     - within ``--radius-km`` of an event's fire position (``lon_c``/
  ``lat_c``) inside that event's window (onset - 1 d to onset + 10 d), the same
  rule as the event super-obs of ``pyro-match``;
* ``background`` - everything else,

and averaged to super-obs per (region, category, model step, model cell) of each
PPE source. All members are sampled there through ``pyro.ppe``. Statistics follow
AeroVal (pyaerocom) definitions and are computed per (region, category, period,
PPE source, variable), for category ``all`` = the regional average as well:

    n, mean_obs, mean_mod, bias = mean(m - o), nmb = sum(m - o) / sum(o),
    mnmb = 2/N sum((m - o) / (m + o)), fge = 2/N sum(|m - o| / (m + o)),
    rmse, r (Pearson), r_spearman

for the ensemble median, the control member (if any) and every member
(``member_stats``; summarised as the 5 / 50 / 95 % of each metric across members).
``period`` is ``season`` or a month ``YYYY-MM``.

Outputs (``$PYRO_DATA_ROOT/derived/regional_stats``, restricted because the
categories derive from the event list): ``superobs_<key>``, ``model_<key>``,
``stats``, ``member_stats``, ``member_summary`` (parquet, plus csv for the stats)
and ``figures/``.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tomllib
from pathlib import Path

import polars as pl

from pyro import compare, ppe
from pyro.events import add_windows, load_events
from pyro.match import SPEX_COLUMNS, spexone_files

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGIONS = REPO_ROOT / "config" / "regions.toml"
EARTH_RADIUS_KM = 6371.0
CATEGORIES = ("background", "pyrocb")
KEYS = ("region", "category")
METRICS = ("n", "mean_obs", "mean_mod", "bias", "nmb", "mnmb", "fge", "rmse", "r", "r_spearman")
SUMMARY_METRICS = ("bias", "nmb", "mnmb", "fge", "rmse", "r")


# ------------------------------------------------------------------ regions


def load_regions(path: str | os.PathLike | None = None) -> pl.DataFrame:
    raw = tomllib.loads(Path(path or DEFAULT_REGIONS).read_text())["regions"]
    rows = [
        {
            "region": name,
            "lat_min": float(r["lat"][0]),
            "lat_max": float(r["lat"][1]),
            "lon_min": float(r["lon"][0]),
            "lon_max": float(r["lon"][1]),
        }
        for name, r in raw.items()
    ]
    df = pl.DataFrame(rows)
    for a in df.iter_rows(named=True):
        for b in df.iter_rows(named=True):
            if a["region"] < b["region"] and not (
                a["lat_max"] <= b["lat_min"]
                or b["lat_max"] <= a["lat_min"]
                or a["lon_max"] <= b["lon_min"]
                or b["lon_max"] <= a["lon_min"]
            ):
                raise ValueError(f"regions {a['region']!r} and {b['region']!r} overlap")
    return df


def assign_region(pix: pl.DataFrame, regions: pl.DataFrame) -> pl.DataFrame:
    """Add ``region`` (box containing the pixel) and drop pixels outside every box."""
    lon = ((pl.col("lon") + 180.0) % 360.0) - 180.0
    expr = pl.lit(None, dtype=pl.String)
    for r in regions.iter_rows(named=True):
        inside = pl.col("lat").is_between(
            r["lat_min"], r["lat_max"], closed="left"
        ) & lon.is_between(r["lon_min"], r["lon_max"], closed="left")
        expr = pl.when(inside).then(pl.lit(r["region"])).otherwise(expr)
    return pix.with_columns(expr.alias("region")).filter(pl.col("region").is_not_null())


def load_pixels(root: Path, regions: pl.DataFrame, start, end) -> pl.DataFrame:
    """SPEXone pixels (land + ocean) in any region box between ``start`` and ``end``."""
    files = spexone_files(root)
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    files = files.filter(pl.col("month").is_in(months))
    if files.is_empty():
        return pl.DataFrame()
    lat_lo, lat_hi = regions["lat_min"].min(), regions["lat_max"].max()
    frames = [
        pl.scan_parquet(path)
        .select(SPEX_COLUMNS)
        .filter(
            pl.col("lat").is_between(lat_lo, lat_hi)
            & pl.col("date").is_between(start, end)
            & pl.col("aot_550nm").is_not_null()
            & pl.col("aot_550nm").is_not_nan()
        )
        .with_columns(pl.lit(surface).alias("surface"))
        for path, surface in files.select("path", "surface").iter_rows()
    ]
    return assign_region(pl.concat(pl.collect_all(frames), how="vertical"), regions)


def _dist_km(lat0: float, lon0: float) -> pl.Expr:
    p1, p2 = math.radians(lat0), pl.col("lat").radians()
    dlmb = (pl.col("lon") - lon0).radians()
    a = ((p2 - p1) / 2).sin() ** 2 + math.cos(p1) * p2.cos() * (dlmb / 2).sin() ** 2
    return 2 * EARTH_RADIUS_KM * a.sqrt().arcsin()


def classify(pix: pl.DataFrame, events: pl.DataFrame, radius_km: float) -> pl.DataFrame:
    """Add ``category``, ``pcb_id`` (nearest affecting event) and ``dist_km`` to it.

    A pixel is ``pyrocb`` when it lies within ``radius_km`` of any event's fire
    position inside that event's window, whatever region the event belongs to.
    """
    pix = pix.with_row_index("_pix")
    hits = []
    dlat = radius_km / 111.0
    for ev in events.iter_rows(named=True):
        coslat = max(math.cos(math.radians(ev["lat_c"])), 0.05)
        dlon = min(radius_km / (111.0 * coslat), 180.0)
        wrapped = ((pl.col("lon") - ev["lon_c"] + 180.0) % 360.0) - 180.0
        sub = (
            pix.lazy()
            .filter(
                pl.col("date").is_between(ev["window_start"], ev["window_end"])
                & (pl.col("lat") - ev["lat_c"]).abs().le(dlat)
                & wrapped.abs().le(dlon)
            )
            .select("_pix", _dist_km(ev["lat_c"], ev["lon_c"]).alias("dist_km"))
            .filter(pl.col("dist_km") <= radius_km)
            .with_columns(pl.lit(ev["pcb_id"]).alias("pcb_id"))
        )
        hits.append(sub)
    near = (
        pl.concat(pl.collect_all(hits))
        .sort("dist_km")
        .group_by("_pix", maintain_order=True)
        .agg(pl.col("pcb_id").first(), pl.col("dist_km").first(), pl.len().alias("n_events"))
        if hits
        else pl.DataFrame(
            schema={
                "_pix": pl.UInt32,
                "pcb_id": pl.String,
                "dist_km": pl.Float64,
                "n_events": pl.UInt32,
            }
        )
    )
    return (
        pix.join(near, on="_pix", how="left")
        .with_columns(
            pl.when(pl.col("pcb_id").is_not_null())
            .then(pl.lit("pyrocb"))
            .otherwise(pl.lit("background"))
            .alias("category"),
            pl.col("n_events").fill_null(0),
        )
        .drop("_pix")
    )


# --------------------------------------------------------------- statistics


def aeroval_exprs(obs: str = "obs", mod: str = "mod") -> list[pl.Expr]:
    o, m = pl.col(obs), pl.col(mod)
    d = m - o
    return [
        pl.len().alias("n"),
        o.mean().alias("mean_obs"),
        m.mean().alias("mean_mod"),
        d.mean().alias("bias"),
        (d.sum() / o.sum()).alias("nmb"),
        (2 * d / (m + o)).mean().alias("mnmb"),
        (2 * d.abs() / (m + o)).mean().alias("fge"),
        (d**2).mean().sqrt().alias("rmse"),
        pl.corr(o, m).alias("r"),
        pl.corr(o, m, method="spearman").alias("r_spearman"),
    ]


def stats(obs_model: pl.DataFrame, group: tuple[str, ...] = ()) -> pl.DataFrame:
    """AeroVal statistics of ``mod`` vs ``obs`` per (region, category, period, dataset,
    variable, *group); category ``all`` and period ``season`` are added as extra
    groupings. Rows with a missing or non-finite value are dropped first."""
    valid = obs_model.lazy().filter(pl.col("obs").is_finite() & pl.col("mod").is_finite())
    valid = valid.with_columns(pl.col("obs_time").dt.strftime("%Y-%m").alias("period"))
    base = ["region", "dataset", "variable", *group]
    keys = ["region", "category", "period", "dataset", "variable", *group]
    passes = []
    for by_cat in (True, False):
        for by_month in (True, False):
            k = base + (["category"] if by_cat else []) + (["period"] if by_month else [])
            agg = valid.group_by(k).agg(aeroval_exprs())
            if not by_cat:
                agg = agg.with_columns(pl.lit("all").alias("category"))
            if not by_month:
                agg = agg.with_columns(pl.lit("season").alias("period"))
            passes.append(agg.select(*keys, *METRICS))
    return pl.concat(pl.collect_all(passes)).sort(keys)


def _obs_long(so: pl.DataFrame) -> pl.DataFrame:
    parts = [
        so.select(
            "point_id",
            "region",
            "category",
            "obs_time",
            pl.lit(var).alias("variable"),
            pl.col(ocol).cast(pl.Float64).alias("obs"),
        )
        for var, (ocol, _, _) in compare.VARIABLES.items()
        if ocol in so.columns
    ]
    return pl.concat(parts)


def _model_long(model: pl.LazyFrame) -> pl.LazyFrame:
    names = set(model.collect_schema().names())
    parts = [
        model.select(
            "point_id",
            "member",
            "is_control",
            pl.lit(var).alias("variable"),
            pl.col(mcol).cast(pl.Float64).alias("mod"),
        )
        for var, (_, mcol, _) in compare.VARIABLES.items()
        if mcol in names
    ]
    return pl.concat(parts)


def evaluate(so: pl.DataFrame, model: pl.DataFrame | pl.LazyFrame, dataset: str):
    """(stats, member_stats) for one PPE source.

    ``stats`` has one row per group and ``estimate`` in {ensemble_median, control};
    ``member_stats`` one row per group and member.
    """
    so = so.with_row_index("point_id").with_columns(pl.col("point_id").cast(pl.Int64))
    obs = _obs_long(so).lazy()
    mod = _model_long(model.lazy())
    joined = mod.join(obs, on=["point_id", "variable"], how="inner").with_columns(
        pl.lit(dataset).alias("dataset")
    )
    med = (
        joined.group_by("point_id", "variable")
        .agg(
            pl.col("mod").median(),
            pl.col("region", "category", "obs_time", "obs", "dataset").first(),
        )
        .with_columns(pl.lit("ensemble_median").alias("estimate"))
    )
    ctrl = (
        joined.filter(pl.col("is_control"))
        .drop("member", "is_control")
        .with_columns(pl.lit("control").alias("estimate"))
    )
    both = pl.concat([med, ctrl.select(med.collect_schema().names())]).collect()
    st = stats(both, group=("estimate",))
    # One variable at a time keeps the (super-obs x members) table small.
    members = pl.concat(
        [
            stats(
                joined.filter(pl.col("variable") == v).drop("is_control").collect(),
                group=("member",),
            )
            for v in compare.VARIABLES
        ]
    )
    return st, members


def summarize_members(member_stats: pl.DataFrame) -> pl.DataFrame:
    """5 / 50 / 95 % across members of each metric, per group."""
    keys = ["region", "category", "period", "dataset", "variable"]
    aggs = [pl.len().alias("n_members"), pl.col("n").median().alias("n")]
    for mname in SUMMARY_METRICS:
        for q in (0.05, 0.5, 0.95):
            aggs.append(pl.col(mname).quantile(q, "linear").alias(f"{mname}_p{round(q * 100):02d}"))
    return member_stats.group_by(keys).agg(aggs).sort(keys)


# ------------------------------------------------------------------ figures


def plot_regions(
    st: pl.DataFrame,
    summary: pl.DataFrame | None = None,
    period: str = "season",
    metrics: tuple[str, ...] = ("nmb", "rmse", "r"),
    variables: list[str] | None = None,
):
    """Bars per region: background vs pyroCb (vs all) for each metric (columns) and
    variable (rows); ensemble median as bar, 5-95 % across members as whiskers."""
    import matplotlib.pyplot as plt
    import numpy as np

    st = st.filter((pl.col("period") == period) & (pl.col("estimate") == "ensemble_median"))
    variables = variables or [v for v in compare.VARIABLES if v in set(st["variable"])]
    regions = sorted(st["region"].unique().to_list())
    datasets = sorted(st["dataset"].unique().to_list())
    cats = ["all", "background", "pyrocb"]
    hatch = {"all": "", "background": "//", "pyrocb": ""}
    alpha = {"all": 0.35, "background": 0.6, "pyrocb": 1.0}
    colors = {d: compare._COLORS[i % len(compare._COLORS)] for i, d in enumerate(datasets)}
    fig, axes = plt.subplots(
        len(variables),
        len(metrics),
        squeeze=False,
        layout="constrained",
        figsize=(4.2 * len(metrics), 2.6 * len(variables) + 0.5),
    )
    width = 0.8 / (len(datasets) * len(cats))
    x0 = np.arange(len(regions))
    for i, var in enumerate(variables):
        for j, met in enumerate(metrics):
            ax = axes[i][j]
            k = 0
            for ds in datasets:
                for cat in cats:
                    sel = st.filter(
                        (pl.col("variable") == var)
                        & (pl.col("dataset") == ds)
                        & (pl.col("category") == cat)
                    )
                    vals = [sel.filter(pl.col("region") == r)[met].to_list() for r in regions]
                    y = np.array([v[0] if v else np.nan for v in vals], dtype=float)
                    x = x0 - 0.4 + width * (k + 0.5)
                    yerr = None
                    if summary is not None and met in SUMMARY_METRICS:
                        s = summary.filter(
                            (pl.col("period") == period)
                            & (pl.col("variable") == var)
                            & (pl.col("dataset") == ds)
                            & (pl.col("category") == cat)
                        )
                        lo, hi = [], []
                        for r, yy in zip(regions, y, strict=True):
                            row = s.filter(pl.col("region") == r)
                            if len(row) and np.isfinite(yy):
                                lo.append(max(yy - row[f"{met}_p05"][0], 0))
                                hi.append(max(row[f"{met}_p95"][0] - yy, 0))
                            else:
                                lo.append(0)
                                hi.append(0)
                        yerr = [lo, hi]
                    ax.bar(
                        x,
                        y,
                        width,
                        color=colors[ds],
                        alpha=alpha[cat],
                        hatch=hatch[cat],
                        edgecolor="k",
                        lw=0.3,
                        yerr=yerr,
                        capsize=1.5,
                        error_kw={"lw": 0.6},
                        label=f"{ds}: {cat}",
                    )
                    k += 1
            ax.axhline(0, color="k", lw=0.5)
            ax.set_xticks(x0, regions, fontsize=8)
            ax.set_title(f"{compare.VARIABLES[var][2]}: {met.upper()}", fontsize=9)
    axes[0][0].legend(fontsize=6, ncols=1)
    fig.suptitle(f"Model vs SPEXone, {period}: regional average (all) vs background vs pyroCb")
    return fig


def plot_scatter(obs_model: pl.DataFrame, region: str, variable: str = "aod550"):
    """AeroVal-style scatter of one region: super-obs vs ensemble median by category."""
    import matplotlib.pyplot as plt

    d = obs_model.filter((pl.col("region") == region) & (pl.col("variable") == variable))
    fig, ax = plt.subplots(figsize=(4.5, 4.5), layout="constrained")
    for cat, c in (("background", "0.6"), ("pyrocb", "tab:red")):
        s = d.filter(pl.col("category") == cat)
        ax.scatter(s["obs"], s["mod"], s=5, alpha=0.5, color=c, label=f"{cat} ({len(s)})")
    vals = d.select("obs", "mod").drop_nulls()
    if len(vals):
        lo, hi = float(vals.min().min_horizontal()[0]), float(vals.max().max_horizontal()[0])
        ax.plot([lo, hi], [lo, hi], "k-", lw=0.6)
        if variable in compare.LOG_VARIABLES and lo > 0:
            ax.set_xscale("log")
            ax.set_yscale("log")
    ax.set_xlabel(f"SPEXone {compare.VARIABLES[variable][2]}")
    ax.set_ylabel("ensemble median")
    ax.set_title(region)
    ax.legend(fontsize=7)
    return fig


# ---------------------------------------------------------------------- CLI


def run(
    events_path: Path,
    spex_root: Path,
    out: Path,
    sources: list[str],
    *,
    regions_path: Path | None = None,
    radius_km: float = 500.0,
    pre_days: float = 1.0,
    post_days: float = 10.0,
    start=None,
    end=None,
    ppe_config: Path | None = None,
    figures: bool = True,
) -> dict[str, pl.DataFrame]:
    from pyro import match

    regions = load_regions(regions_path)
    events = add_windows(load_events(events_path), pre_days, post_days)
    events = events.filter(pl.col("region").is_in(regions["region"].to_list()))
    start = start or events["window_start"].min()
    end = end or events["window_end"].max()
    pix = load_pixels(spex_root, regions, start, end)
    print(
        f"{len(pix)} SPEXone pixels in {regions.height} regions, {start} - {end}", file=sys.stderr
    )
    pix = classify(pix, events, radius_km).with_columns(
        pl.lit(0.0).alias("dt_hours"), pl.col("dist_km").fill_null(-1.0)
    )
    print(pix.group_by("region", "category").len().sort("region", "category"), file=sys.stderr)
    out.mkdir(parents=True, exist_ok=True)
    all_st, all_mem = [], []
    for key in sources:
        src = ppe.open_source(key, ppe_config)
        so = match.superobs(
            pix.with_columns(pl.lit(0.0).alias("dist_km")), src, math.inf, keys=KEYS
        )
        print(f"{key}: {len(so)} regional super-obs", file=sys.stderr)
        if so.is_empty():
            continue
        model = match.sample_model(so, src, keys=KEYS)
        so.write_parquet(out / f"superobs_{key}.parquet", compression="zstd")
        model.write_parquet(out / f"model_{key}.parquet", compression="zstd")
        st, mem = evaluate(so, model, key)
        all_st.append(st)
        all_mem.append(mem)
    if not all_st:
        return {}
    st = pl.concat(all_st, how="diagonal")
    mem = pl.concat(all_mem, how="diagonal")
    summ = summarize_members(mem)
    st.write_parquet(out / "stats.parquet", compression="zstd")
    st.write_csv(out / "stats.csv")
    mem.write_parquet(out / "member_stats.parquet", compression="zstd")
    summ.write_parquet(out / "member_summary.parquet", compression="zstd")
    summ.write_csv(out / "member_summary.csv")
    if figures:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figdir = out / "figures"
        figdir.mkdir(exist_ok=True)
        fig = plot_regions(st, summ)
        fig.savefig(figdir / "regions_season.png", dpi=120)
        plt.close(fig)
    return {"stats": st, "member_stats": mem, "member_summary": summ}


def main(argv: list[str] | None = None) -> int:
    from pyro.envfile import load_env_file

    p = argparse.ArgumentParser(prog="pyro-regional", description=__doc__.splitlines()[0])
    p.add_argument("events", type=Path, help="event table (CSV or parquet)")
    p.add_argument("--spexone-root", type=Path, default=None)
    p.add_argument("--regions", type=Path, default=None, help="default config/regions.toml")
    p.add_argument(
        "--ppe",
        action="append",
        default=None,
        help="PPE source key (repeatable; default ybhatti_pd_3h)",
    )
    p.add_argument(
        "--allow-coarse",
        action="store_true",
        help="allow sources coarser than 3-hourly (flagged by 'dataset')",
    )
    p.add_argument("--ppe-config", type=Path, default=None)
    p.add_argument("--radius-km", type=float, default=500.0)
    p.add_argument("--pre-days", type=float, default=1.0)
    p.add_argument("--post-days", type=float, default=10.0)
    p.add_argument("--start", default=None, help="UTC date; default first event window start")
    p.add_argument("--end", default=None, help="UTC date; default last event window end")
    p.add_argument("-o", "--out", type=Path, default=None)
    p.add_argument("--no-figures", action="store_true")
    p.add_argument("--env-file", default=str(REPO_ROOT / "config" / "local.env"))
    a = p.parse_args(argv)
    if Path(a.env_file).exists():
        load_env_file(a.env_file)
    spex = a.spexone_root or os.environ.get("SPEXONE_EXTRACT_ROOT")
    if not spex:
        p.error("--spexone-root or SPEXONE_EXTRACT_ROOT is required")
    root = os.environ.get("PYRO_DATA_ROOT")
    if a.out is None and not root:
        p.error("--out or PYRO_DATA_ROOT is required")
    out = a.out or Path(root) / "derived" / "regional_stats"
    sources = a.ppe or ["ybhatti_pd_3h"]
    cfg = ppe.load_config(a.ppe_config)
    for key in sources:
        if key not in cfg:
            p.error(f"unknown PPE source {key!r}; configured: {sorted(cfg)}")
        if cfg[key].get("resolution") != "3h" and not a.allow_coarse:
            p.error(f"{key} is {cfg[key].get('resolution')}; add --allow-coarse")

    def _t(s):
        return pl.Series([s]).str.to_datetime(time_zone="UTC")[0] if s else None

    res = run(
        a.events,
        Path(spex),
        out,
        sources,
        regions_path=a.regions,
        radius_km=a.radius_km,
        pre_days=a.pre_days,
        post_days=a.post_days,
        start=_t(a.start),
        end=_t(a.end),
        ppe_config=a.ppe_config,
        figures=not a.no_figures,
    )
    if res:
        season = (
            res["stats"]
            .filter((pl.col("period") == "season") & (pl.col("estimate") == "ensemble_median"))
            .select("dataset", "region", "category", "variable", "n", "bias", "nmb", "rmse", "r")
        )
        with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200):
            print(season)
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
