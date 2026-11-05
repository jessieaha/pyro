"""Event-table loading, validation and comparison windows.

The fire-side event table (pyroCb events and, later, lookalike controls) is
external, restricted data: it lives under ``$PYRO_DATA_ROOT/events`` and is
never committed. Collocation uses the matched fire position ``lon_c``/``lat_c``,
not the reported (0.1 deg rounded) ``lon``/``lat``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

REQUIRED_COLUMNS = ("pcb_id", "onset_utc", "region", "lon_c", "lat_c", "kept")
BOOL_COLUMNS = ("kept", "is_confirmed")
ONSET_FORMAT = "%Y-%m-%d %H:%M:%S"
UTC_DATETIME = pl.Datetime("us", "UTC")

# Temporal coverage of the data products (end exclusive), as inventoried on
# Snellius 2026-11-03; see docs/data_inventory.md. None = open ended.
PERIODS: dict[str, tuple[str, str | None]] = {
    "ppe_pd2024_3h": ("2024-08-01", "2025-08-01"),
    "mmppe_2025_6h": ("2025-01-01", "2026-01-01"),
    "spexone_local": ("2024-06-01", "2026-01-01"),
    "atlid_l3_local": ("2024-08-01", "2025-08-01"),
    "atlid_l2_public": ("2025-03-11", None),
}

_BOOL_STRINGS = {"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}


class EventTableError(ValueError):
    pass


def _to_bool(s: pl.Series) -> pl.Series:
    if s.dtype == pl.Boolean:
        if s.null_count():
            raise EventTableError(f"missing values in {s.name}")
        return s
    out = s.cast(pl.String).str.strip_chars().str.to_lowercase()
    out = out.replace_strict(_BOOL_STRINGS, default=None, return_dtype=pl.Boolean)
    if out.null_count():
        bad = s.filter(out.is_null()).unique(maintain_order=True).head(5).to_list()
        raise EventTableError(f"non-boolean values in {s.name}: {bad}")
    return out


def _to_utc(s: pl.Series) -> pl.Series:
    if s.dtype == pl.String:
        try:
            return s.str.to_datetime(ONSET_FORMAT, time_zone="UTC", time_unit="us")
        except pl.exceptions.InvalidOperationError:
            return s.str.to_datetime(time_zone="UTC", time_unit="us")
    if isinstance(s.dtype, pl.Datetime):
        if s.dtype.time_zone is None:
            return s.dt.replace_time_zone("UTC").cast(UTC_DATETIME)
        return s.dt.convert_time_zone("UTC").cast(UTC_DATETIME)
    raise EventTableError(f"onset_utc has unsupported dtype {s.dtype}")


def load_events(path: str | Path) -> pl.DataFrame:
    """Read and validate an event table (CSV or parquet)."""
    path = Path(path)
    if path.suffix == ".parquet":
        df = pl.read_parquet(path)
    else:
        df = pl.read_csv(path, infer_schema_length=None)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise EventTableError(f"{path.name}: missing columns {missing}")
    dup = df["pcb_id"].is_duplicated()
    if dup.any():
        dups = df.filter(dup)["pcb_id"].unique(maintain_order=True).head(5).to_list()
        raise EventTableError(f"duplicate pcb_id: {dups}")
    try:
        onset = _to_utc(df["onset_utc"])
    except pl.exceptions.PolarsError as exc:
        raise EventTableError(f"cannot parse onset_utc: {exc}") from exc
    df = df.with_columns(onset)
    df = df.with_columns(_to_bool(df[c]) for c in BOOL_COLUMNS if c in df.columns)
    in_range = df.select(
        (pl.col("lon_c").is_between(-180, 180) & pl.col("lat_c").is_between(-90, 90)).all()
    ).item()
    if not in_range:
        raise EventTableError("lon_c/lat_c out of range")
    if "role" not in df.columns:
        df = df.with_columns(role=pl.lit("pyrocb"))
    return df


def add_windows(df: pl.DataFrame, pre_days: float = 1.0, post_days: float = 10.0) -> pl.DataFrame:
    """Comparison window per event: [onset - pre_days, onset + post_days].

    The pre-onset day carries the same-day polar-orbiter overpass, which for
    afternoon onsets usually precedes the pyroCb; post-onset days cover the
    aged, downwind plume.
    """
    return df.with_columns(
        window_start=pl.col("onset_utc") - timedelta(days=pre_days),
        window_end=pl.col("onset_utc") + timedelta(days=post_days),
    )


def _period_bounds(start: str, end: str | None) -> tuple[datetime, datetime | None]:
    s = datetime.fromisoformat(start).replace(tzinfo=UTC)
    e = datetime.fromisoformat(end).replace(tzinfo=UTC) if end else None
    return s, e


def coverage(
    df: pl.DataFrame,
    periods: Mapping[str, tuple[str, str | None]] = PERIODS,
    by: str | None = "region",
) -> pl.DataFrame:
    """Count events whose onset / full window / any part of the window is in each period.

    Counts cover every row of ``df`` (kept or not), as an ``all`` group plus one
    group per value of ``by``; the ``*_kept`` columns repeat each count for the
    ``kept == True`` subset.
    """
    if "window_start" not in df.columns:
        df = add_windows(df)
    key = by or "group"
    onset, ws, we = pl.col("onset_utc"), pl.col("window_start"), pl.col("window_end")
    kept = pl.col("kept")
    frames = []
    for name, (start, end) in periods.items():
        ps, pe = _period_bounds(start, end)
        flags = {
            "onset_in": (onset >= ps) & (onset < pe) if pe else onset >= ps,
            "window_full": (ws >= ps) & (we <= pe) if pe else ws >= ps,
            "window_any": (we > ps) & (ws < pe) if pe else we > ps,
        }
        aggs = [pl.len().alias("n_events"), kept.sum().alias("n_kept")]
        for col, cond in flags.items():
            aggs += [cond.sum().alias(col), (cond & kept).sum().alias(f"{col}_kept")]
        total = df.select(aggs).with_columns(pl.lit("all").alias(key))
        parts = [total]
        if by is not None:
            parts.append(
                df.group_by(by)
                .agg(aggs)
                .sort(by)
                .with_columns(pl.col(by).cast(pl.String).alias(key))
            )
        frames.append(pl.concat(parts, how="diagonal").with_columns(period=pl.lit(name)))
    cols = ["period", key, "n_events", "n_kept"]
    cols += [c for f in ("onset_in", "window_full", "window_any") for c in (f, f"{f}_kept")]
    return pl.concat(frames).select(cols).with_columns(pl.col(cols[2:]).cast(pl.Int64))


def write_table(df: pl.DataFrame, path: str | Path) -> Path:
    """Write ``df`` as CSV if ``path`` ends in ``.csv``, otherwise as parquet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".csv":
        df.write_csv(path)
    else:
        df.write_parquet(path)
    return path


def _parse_periods(items: Sequence[str]) -> dict[str, tuple[str, str | None]]:
    periods = dict(PERIODS)
    for item in items:
        name, _, span = item.partition("=")
        start, _, end = span.partition("/")
        if not name or not start:
            raise SystemExit(f"--period expects name=YYYY-MM-DD/YYYY-MM-DD, got {item!r}")
        periods[name] = (start, end or None)
    return periods


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pyro-events", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    cov = sub.add_parser("coverage", help="event overlap with model / satellite periods")
    cov.add_argument("events")
    cov.add_argument("--pre-days", type=float, default=1.0)
    cov.add_argument("--post-days", type=float, default=10.0)
    cov.add_argument(
        "--all",
        action="store_true",
        help="no-op, kept for compatibility: coverage always counts all rows (*_kept columns)",
    )
    cov.add_argument("--period", action="append", default=[], help="name=START/END (add/override)")

    win = sub.add_parser("windows", help="write per-event comparison windows (.parquet or .csv)")
    win.add_argument("events")
    win.add_argument("-o", "--output", required=True)
    win.add_argument("--pre-days", type=float, default=1.0)
    win.add_argument("--post-days", type=float, default=10.0)
    win.add_argument("--all", action="store_true", help="include rows with kept == False")

    args = p.parse_args(argv)
    try:
        df = load_events(args.events)
    except EventTableError as exc:
        print(f"pyro-events: error: {exc}", file=sys.stderr)
        return 2
    df = add_windows(df, args.pre_days, args.post_days)

    if args.command == "coverage":
        table = coverage(df, _parse_periods(args.period))
        with pl.Config(
            tbl_rows=200,
            tbl_cols=-1,
            tbl_width_chars=160,
            tbl_hide_dataframe_shape=True,
            tbl_hide_column_data_types=True,
        ):
            print(table)
        return 0

    if not args.all:
        df = df.filter(pl.col("kept"))
    out = write_table(df, args.output)
    print(f"wrote {df.height} event windows to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
