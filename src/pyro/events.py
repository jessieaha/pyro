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
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = ("pcb_id", "onset_utc", "region", "lon_c", "lat_c", "kept")
BOOL_COLUMNS = ("kept", "is_confirmed")

# Temporal coverage of the data products (end exclusive), as inventoried on
# Snellius 2026-11-03; see docs/data_inventory.md. None = open ended.
PERIODS: dict[str, tuple[str, str | None]] = {
    "ppe_pd2024_3h": ("2024-08-01", "2025-08-01"),
    "mmppe_2025_6h": ("2025-01-01", "2026-01-01"),
    "spexone_local": ("2024-06-01", "2026-01-01"),
    "atlid_l3_local": ("2024-08-01", "2025-08-01"),
    "atlid_l2_public": ("2025-03-11", None),
}


class EventTableError(ValueError):
    pass


def _to_bool(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    mapping = {"true": True, "false": False, "1": True, "0": False, "yes": True, "no": False}
    out = s.astype(str).str.strip().str.lower().map(mapping)
    if out.isna().any():
        bad = s[out.isna()].unique()[:5]
        raise EventTableError(f"non-boolean values in {s.name}: {list(bad)}")
    return out.astype(bool)


def load_events(path: str | Path) -> pd.DataFrame:
    """Read and validate an event table (CSV or parquet)."""
    path = Path(path)
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise EventTableError(f"{path.name}: missing columns {missing}")
    if df["pcb_id"].duplicated().any():
        dups = df.loc[df["pcb_id"].duplicated(), "pcb_id"].tolist()[:5]
        raise EventTableError(f"duplicate pcb_id: {dups}")
    df = df.copy()
    df["onset_utc"] = pd.to_datetime(df["onset_utc"], utc=True)
    for col in BOOL_COLUMNS:
        if col in df.columns:
            df[col] = _to_bool(df[col])
    if not df["lon_c"].between(-180, 180).all() or not df["lat_c"].between(-90, 90).all():
        raise EventTableError("lon_c/lat_c out of range")
    if "role" not in df.columns:
        df["role"] = "pyrocb"
    return df


def add_windows(df: pd.DataFrame, pre_days: float = 1.0, post_days: float = 10.0) -> pd.DataFrame:
    """Comparison window per event: [onset - pre_days, onset + post_days].

    The pre-onset day carries the same-day polar-orbiter overpass, which for
    afternoon onsets usually precedes the pyroCb; post-onset days cover the
    aged, downwind plume.
    """
    out = df.copy()
    out["window_start"] = out["onset_utc"] - pd.to_timedelta(pre_days, unit="D")
    out["window_end"] = out["onset_utc"] + pd.to_timedelta(post_days, unit="D")
    return out


def _period_bounds(start: str, end: str | None) -> tuple[pd.Timestamp, pd.Timestamp]:
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.max.tz_localize("UTC")
    return s, e


def coverage(
    df: pd.DataFrame,
    periods: Mapping[str, tuple[str, str | None]] = PERIODS,
    by: str | None = "region",
) -> pd.DataFrame:
    """Count events whose onset / full window / any part of the window is in each period."""
    if "window_start" not in df.columns:
        df = add_windows(df)
    rows = []
    groups = [("all", df)] if by is None else [("all", df), *df.groupby(by)]
    for name, (start, end) in periods.items():
        ps, pe = _period_bounds(start, end)
        for key, g in groups:
            onset = (g["onset_utc"] >= ps) & (g["onset_utc"] < pe)
            full = (g["window_start"] >= ps) & (g["window_end"] <= pe)
            anyo = (g["window_end"] > ps) & (g["window_start"] < pe)
            rows.append(
                {
                    "period": name,
                    by or "group": key,
                    "n_events": len(g),
                    "onset_in": int(onset.sum()),
                    "window_full": int(full.sum()),
                    "window_any": int(anyo.sum()),
                }
            )
    return pd.DataFrame(rows)


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
    cov.add_argument("--all", action="store_true", help="include rows with kept == False")
    cov.add_argument("--period", action="append", default=[], help="name=START/END (add/override)")

    win = sub.add_parser("windows", help="write per-event comparison windows to parquet")
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
    if not args.all:
        df = df[df["kept"]]
    df = add_windows(df, args.pre_days, args.post_days)

    if args.command == "coverage":
        table = coverage(df, _parse_periods(args.period))
        with pd.option_context("display.width", 120, "display.max_rows", 200):
            print(table.to_string(index=False))
        return 0

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"wrote {len(df)} event windows to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
