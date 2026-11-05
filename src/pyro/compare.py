"""Per-event comparison of satellite super-obs with PPE ensembles (step 1f).

Reads the ``pyro-match`` outputs (``summary``, ``pixels``, ``superobs_<key>``,
``model_<key>``; common ``pyro.ppe`` schema) and, for every pyroCb event:

* reduces each PPE source to ensemble statistics per super-obs (median, 5-95 %
  range, control member where one exists),
* puts observation and model side by side in one long table
  (event, dataset, super-obs, variable),
* scores each (event, dataset, variable): bias, log ratio, share of observations
  inside the ensemble 5-95 % range,
* draws one figure per event (pixel map + time series against plume age) and an
  all-event scatter.

``pyro-compare`` writes all of this to ``$PYRO_DATA_ROOT/derived/event_comparison``;
``notebooks/event_comparison.ipynb`` uses the same functions interactively.
Every output is keyed by event and therefore restricted (never committed).

Observations are SPEXone today. Another product (VIIRS) joins by writing
``superobs_<key>.parquet`` files with the same columns and a ``product`` column.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import polars as pl

# variable -> (observation column, canonical PPE variable, label)
VARIABLES = {
    "aod550": ("o_aod550", "od550aer", "AOD 550 nm"),
    "aaod550": ("o_aaod550", "abs550aer", "AAOD 550 nm"),
    "ssa550": ("o_ssa550", "ssa550aer", "SSA 550 nm"),
    "ae440_670": ("o_ae440_670", "ae440_670", "AE 440-670 nm"),
}
# Log ratios are only meaningful for strictly positive quantities.
LOG_VARIABLES = ("aod550", "aaod550")
QUANTILES = (0.05, 0.5, 0.95)
SUPEROBS_RE = re.compile(r"^superobs_(.+)\.parquet$")


@dataclass
class Comparison:
    summary: pl.DataFrame
    table: pl.DataFrame
    pixels_path: Path | None

    @property
    def events(self) -> list[str]:
        return sorted(self.table["pcb_id"].unique().to_list()) if len(self.table) else []

    def pixels(self, pcb_id: str) -> pl.DataFrame:
        if self.pixels_path is None or not self.pixels_path.exists():
            return pl.DataFrame()
        return pl.scan_parquet(self.pixels_path).filter(pl.col("pcb_id") == pcb_id).collect()


def discover_sources(match_dir: Path) -> list[str]:
    keys = []
    for f in sorted(match_dir.glob("superobs_*.parquet")):
        m = SUPEROBS_RE.match(f.name)
        if m and (match_dir / f"model_{m.group(1)}.parquet").exists():
            keys.append(m.group(1))
    return keys


def ensemble_stats(model: pl.LazyFrame | pl.DataFrame, variables: list[str]) -> pl.DataFrame:
    """One row per ``point_id``: quantiles over members, control value, member count."""
    lf = model.lazy()
    names = set(lf.collect_schema().names())
    aggs = [pl.col("member").n_unique().alias("n_members")]
    for v in variables:
        if v not in names:
            continue
        for q in QUANTILES:
            aggs.append(pl.col(v).quantile(q, "linear").alias(f"{v}__q{round(q * 100):02d}"))
        aggs.append(pl.col(v).filter(pl.col("is_control")).first().alias(f"{v}__ctrl"))
    return lf.group_by("point_id").agg(aggs).collect()


def _long(so: pl.DataFrame, stats: pl.DataFrame, key: str) -> pl.DataFrame:
    df = so.join(stats, on="point_id", how="inner")
    base = [
        "pcb_id",
        "point_id",
        "obs_time",
        "model_time",
        "model_lat",
        "model_lon",
        "dist_km",
        "n_pix",
        "surface",
        "n_members",
    ]
    meta = {
        "dataset": key,
        "ppe_source": so["ppe_source"][0] if "ppe_source" in so.columns else None,
        "resolution": so["resolution"][0] if "resolution" in so.columns else None,
        "product": so["product"][0] if "product" in so.columns else "SPEXone",
    }
    parts = []
    for var, (ocol, mcol, _) in VARIABLES.items():
        if ocol not in df.columns or f"{mcol}__q50" not in df.columns:
            continue
        unc = "o_aod550_unc" if var == "aod550" and "o_aod550_unc" in df.columns else None
        parts.append(
            df.select(
                *[c for c in base if c in df.columns],
                pl.lit(var).alias("variable"),
                pl.col(ocol).cast(pl.Float64).alias("obs"),
                (pl.col(unc) if unc else pl.lit(None)).cast(pl.Float64).alias("obs_unc"),
                pl.col(f"{mcol}__q05").cast(pl.Float64).alias("model_p05"),
                pl.col(f"{mcol}__q50").cast(pl.Float64).alias("model_median"),
                pl.col(f"{mcol}__q95").cast(pl.Float64).alias("model_p95"),
                pl.col(f"{mcol}__ctrl").cast(pl.Float64).alias("model_control"),
            )
        )
    if not parts:
        return pl.DataFrame()
    return pl.concat(parts, how="diagonal").with_columns(
        *[pl.lit(v, dtype=pl.String).alias(k) for k, v in meta.items()]
    )


def load(match_dir: str | os.PathLike, sources: list[str] | None = None) -> Comparison:
    """Observation-vs-ensemble table for every event and PPE source in ``match_dir``."""
    match_dir = Path(match_dir)
    summary = pl.read_parquet(match_dir / "summary.parquet")
    keys = sources or discover_sources(match_dir)
    want = [m for _, m, _ in VARIABLES.values()]
    tables = []
    for key in keys:
        so = pl.read_parquet(match_dir / f"superobs_{key}.parquet").with_row_index("point_id")
        so = so.with_columns(pl.col("point_id").cast(pl.Int64))
        stats = ensemble_stats(pl.scan_parquet(match_dir / f"model_{key}.parquet"), want)
        t = _long(so, stats, key)
        if len(t):
            tables.append(t)
    table = pl.concat(tables, how="diagonal") if tables else pl.DataFrame()
    if len(table):
        table = (
            table.join(
                summary.select("pcb_id", "region", "kept", "onset_utc", "lat_c", "lon_c"),
                on="pcb_id",
                how="left",
            )
            .with_columns(
                (
                    (
                        pl.col("obs_time").cast(pl.Datetime("us", "UTC")) - pl.col("onset_utc")
                    ).dt.total_seconds()
                    / 86400.0
                ).alias("age_days")
            )
            .sort("pcb_id", "dataset", "variable", "obs_time")
        )
    pixels = match_dir / "pixels.parquet"
    return Comparison(summary, table, pixels if pixels.exists() else None)


def metrics(table: pl.DataFrame) -> pl.DataFrame:
    """Scores per (event, dataset, variable) over valid observation/model pairs."""
    if not len(table):
        return pl.DataFrame()
    valid = table.filter(pl.col("obs").is_not_null() & pl.col("model_median").is_not_null())
    pos = (
        pl.col("variable").is_in(LOG_VARIABLES) & (pl.col("obs") > 0) & (pl.col("model_median") > 0)
    )
    logr = pl.when(pos).then((pl.col("model_median") / pl.col("obs")).log10())
    inside = (pl.col("obs") >= pl.col("model_p05")) & (pl.col("obs") <= pl.col("model_p95"))
    return (
        valid.group_by(
            "pcb_id", "region", "kept", "dataset", "ppe_source", "resolution", "product", "variable"
        )
        .agg(
            pl.len().alias("n_superobs"),
            pl.col("n_pix").sum().alias("n_pix"),
            pl.col("age_days").min().alias("age_days_min"),
            pl.col("age_days").max().alias("age_days_max"),
            pl.col("obs").mean().alias("obs_mean"),
            pl.col("model_median").mean().alias("model_median_mean"),
            (pl.col("model_median") - pl.col("obs")).mean().alias("bias"),
            logr.mean().alias("log10_ratio"),
            inside.mean().alias("frac_in_5_95"),
            (pl.col("model_control") - pl.col("obs")).mean().alias("bias_control"),
        )
        .sort("pcb_id", "dataset", "variable")
    )


def coverage(cmp: Comparison) -> pl.DataFrame:
    """Per event: whether each PPE source has collocated super-obs (all events listed)."""
    ev = cmp.summary.select("pcb_id", "region", "kept", "onset_utc", "n_pix_500km")
    if not len(cmp.table):
        return ev
    n = (
        cmp.table.filter(pl.col("variable") == "aod550")
        .group_by("pcb_id", "dataset")
        .agg(pl.len().alias("n"))
        .pivot(on="dataset", index="pcb_id", values="n")
    )
    return ev.join(n, on="pcb_id", how="left").sort("onset_utc")


# ------------------------------------------------------------------ figures

_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]


def _dataset_colors(table: pl.DataFrame) -> dict[str, str]:
    keys = sorted(table["dataset"].unique().to_list()) if len(table) else []
    return {k: _COLORS[i % len(_COLORS)] for i, k in enumerate(keys)}


def plot_event(cmp: Comparison, pcb_id: str, variables: list[str] | None = None):
    """Pixel map (left) and observation vs ensemble against plume age (right)."""
    import matplotlib.pyplot as plt
    import numpy as np

    variables = variables or list(VARIABLES)
    t = cmp.table.filter(pl.col("pcb_id") == pcb_id)
    ev = cmp.summary.filter(pl.col("pcb_id") == pcb_id)
    colors = _dataset_colors(cmp.table)
    fig = plt.figure(figsize=(13, 2.4 * len(variables) + 0.6), layout="constrained")
    gs = fig.add_gridspec(len(variables), 2, width_ratios=[1, 1.6])

    ax_map = fig.add_subplot(gs[:, 0])
    pix = cmp.pixels(pcb_id)
    if len(ev):
        lat_c, lon_c = ev["lat_c"][0], ev["lon_c"][0]
        if len(pix):
            dlon = ((pix["lon"].to_numpy() - lon_c + 180) % 360) - 180
            sc = ax_map.scatter(
                lon_c + dlon,
                pix["lat"],
                c=pix["aot_550nm"],
                s=4,
                cmap="magma_r",
                vmin=0,
                vmax=max(1.0, float(np.nanpercentile(pix["aot_550nm"], 98))),
            )
            fig.colorbar(sc, ax=ax_map, shrink=0.6, label="SPEXone AOD 550 nm")
        for r in (150, 500):
            th = np.linspace(0, 2 * np.pi, 200)
            ax_map.plot(
                lon_c + r / (111.2 * np.cos(np.radians(lat_c))) * np.cos(th),
                lat_c + r / 111.2 * np.sin(th),
                "k--",
                lw=0.6,
            )
        ax_map.plot(lon_c, lat_c, marker="*", color="red", ms=14, mec="k")
        ax_map.set_aspect(1 / np.cos(np.radians(lat_c)))
        onset = ev["onset_utc"][0]
        ax_map.set_title(
            f"{pcb_id}  {ev['region'][0]}\nonset {onset:%Y-%m-%d %H:%M} UTC, "
            f"kept={ev['kept'][0]}, {len(pix)} pixels"
        )
    ax_map.set_xlabel("lon")
    ax_map.set_ylabel("lat")

    for i, var in enumerate(variables):
        ax = fig.add_subplot(gs[i, 1])
        tv = t.filter(pl.col("variable") == var)
        groups = sorted(tv.partition_by("dataset", as_dict=True).items())
        for j, (key, d) in enumerate(groups):
            key = key[0]
            x = d["age_days"].to_numpy() + 0.04 * (j + 1)
            coarse = d["resolution"][0] not in ("3h", None)
            # Super-obs are averaged per model step and cell, so each source has its own.
            ax.errorbar(
                d["age_days"],
                d["obs"],
                yerr=d["obs_unc"].fill_null(0) if var == "aod550" and not coarse else None,
                fmt="o",
                ms=4 if not coarse else 3,
                lw=0.8,
                zorder=3 if not coarse else 2,
                color="k" if not coarse else "0.6",
                mfc="k" if not coarse else "none",
                label=f"{d['product'][0]} super-obs ({key} cells)",
            )
            lab = f"{key} median, 5-95%" + (" (context)" if coarse else "")
            ax.errorbar(
                x,
                d["model_median"],
                yerr=[d["model_median"] - d["model_p05"], d["model_p95"] - d["model_median"]],
                fmt="s" if not coarse else "^",
                color=colors[key],
                ms=3,
                lw=0.8,
                alpha=0.5 if coarse else 0.9,
                label=lab,
            )
            if d["model_control"].is_not_null().any():
                ax.plot(x, d["model_control"], "x", color=colors[key], ms=4, label=f"{key} control")
        ax.axvline(0, color="grey", lw=0.6)
        ax.set_ylabel(VARIABLES[var][2])
        if var in LOG_VARIABLES and len(tv) and (tv["obs"].drop_nulls() > 0).all():
            ax.set_yscale("log")
        if i == 0:
            ax.legend(fontsize=7, loc="best")
        if i == len(variables) - 1:
            ax.set_xlabel("days since onset")
    return fig


def plot_overview(table: pl.DataFrame, variables: list[str] | None = None):
    """Observed vs ensemble-median super-obs over all events, one panel per variable."""
    import matplotlib.pyplot as plt
    import numpy as np

    variables = variables or list(VARIABLES)
    colors = _dataset_colors(table)
    fig, axes = plt.subplots(
        1, len(variables), figsize=(4 * len(variables), 4), layout="constrained", squeeze=False
    )
    for ax, var in zip(axes[0], variables, strict=True):
        tv = table.filter(pl.col("variable") == var)
        for key, d in tv.partition_by("dataset", as_dict=True).items():
            ax.scatter(
                d["obs"], d["model_median"], s=6, alpha=0.5, color=colors[key[0]], label=key[0]
            )
        vals = tv.select("obs", "model_median").drop_nulls()
        if len(vals):
            lo = float(np.nanmin(vals.to_numpy()))
            hi = float(np.nanmax(vals.to_numpy()))
            ax.plot([lo, hi], [lo, hi], "k-", lw=0.6)
            if var in LOG_VARIABLES and lo > 0:
                ax.set_xscale("log")
                ax.set_yscale("log")
        ax.set_xlabel(f"observed {VARIABLES[var][2]}")
        ax.set_ylabel("ensemble median")
    axes[0][0].legend(fontsize=7)
    return fig


# ---------------------------------------------------------------------- CLI


def run(
    match_dir: Path,
    out: Path,
    sources: list[str] | None,
    events: list[str] | None,
    figures: bool = True,
) -> Comparison:
    cmp = load(match_dir, sources)
    out.mkdir(parents=True, exist_ok=True)
    if len(cmp.table):
        cmp.table.write_parquet(out / "comparison.parquet", compression="zstd")
    m = metrics(cmp.table)
    if len(m):
        m.write_parquet(out / "metrics.parquet", compression="zstd")
        m.write_csv(out / "metrics.csv")
    coverage(cmp).write_csv(out / "coverage.csv")
    if figures and len(cmp.table):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        figdir = out / "figures"
        figdir.mkdir(exist_ok=True)
        fig = plot_overview(cmp.table)
        fig.savefig(figdir / "overview.png", dpi=120)
        plt.close(fig)
        for ev in events or cmp.events:
            fig = plot_event(cmp, ev)
            fig.savefig(figdir / f"{ev}.png", dpi=110)
            plt.close(fig)
    return cmp


def main(argv: list[str] | None = None) -> int:
    from pyro.envfile import load_env_file

    p = argparse.ArgumentParser(prog="pyro-compare", description=__doc__.splitlines()[0])
    p.add_argument(
        "--match-dir",
        type=Path,
        default=None,
        help="pyro-match output (default $PYRO_DATA_ROOT/derived/spexone_event_match)",
    )
    p.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="output (default $PYRO_DATA_ROOT/derived/event_comparison)",
    )
    p.add_argument(
        "--ppe",
        action="append",
        default=None,
        help="PPE source key (repeatable; default: every superobs_<key> found)",
    )
    p.add_argument("--event", action="append", default=None, help="only draw these events")
    p.add_argument("--no-figures", action="store_true")
    p.add_argument(
        "--env-file", default=str(Path(__file__).resolve().parents[2] / "config" / "local.env")
    )
    a = p.parse_args(argv)
    if Path(a.env_file).exists():
        load_env_file(a.env_file)
    root = os.environ.get("PYRO_DATA_ROOT")
    if (a.match_dir is None or a.out is None) and not root:
        print("set PYRO_DATA_ROOT or pass --match-dir and --out", file=sys.stderr)
        return 2
    match_dir = a.match_dir or Path(root) / "derived" / "spexone_event_match"
    out = a.out or Path(root) / "derived" / "event_comparison"
    cmp = run(match_dir, out, a.ppe, a.event, figures=not a.no_figures)
    print(
        f"{len(cmp.events)} events with collocated super-obs, "
        f"{cmp.table['dataset'].n_unique() if len(cmp.table) else 0} PPE sources -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
