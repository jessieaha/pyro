"""VIIRS aerosol L2 (Deep Blue, Dark Target): download, pixel extracts, gridding.

Same route as SPEXone:

1. ``fetch``   - granules around each event window from NASA Earthdata
                 (``earthaccess``; credentials from ``EARTHDATA_TOKEN`` or
                 ``EARTHDATA_USERNAME``/``EARTHDATA_PASSWORD``).
2. ``extract`` - granules -> one parquet per product and month with one row per
                 valid pixel: ``time, lat, lon, aod550, ae, aerosol_type, product,
                 platform, granule``.
3. ``grid``    - pixels -> super-obs on the 3-hourly PPE grid around each event
                 (``pyro.gridding.grid_model``, sampled against all members like
                 SPEXone), or regular-degree bins and regional means
                 (``grid_regular`` / ``regional_means``).

The ``*_Best_Estimate`` (Deep Blue) and ``Optical_Depth_Land_And_Ocean`` (Dark
Target) fields are already quality-filtered by the product. VIIRS Angstrom
exponents use other wavelength pairs than the model (Deep Blue land 412-488 nm,
ocean 550-865 nm), so ``ae`` is kept as ``o_ae_native`` and is not compared with
the model's 440-670 nm value directly.

Dataset names are listed per product with alternatives; ``pyro-viirs inspect
FILE`` prints what a granule actually holds, to confirm the mapping on the first
real file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

import numpy as np
import polars as pl

from pyro import gridding, ppe

PRODUCTS = {
    "AERDB_L2_VIIRS_SNPP": "db",
    "AERDB_L2_VIIRS_NOAA20": "db",
    "AERDB_L2_VIIRS_NOAA21": "db",
    "AERDT_L2_VIIRS_SNPP": "dt",
    "AERDT_L2_VIIRS_NOAA20": "dt",
}
# output column -> candidate dataset paths (first one present wins)
FIELDS = {
    "db": {
        "lat": ["Latitude", "latitude", "geolocation_data/latitude"],
        "lon": ["Longitude", "longitude", "geolocation_data/longitude"],
        "time": ["Scan_Start_Time", "scan_start_time"],
        "aod550": ["Aerosol_Optical_Thickness_550_Land_Ocean_Best_Estimate"],
        "ae": ["Angstrom_Exponent_Land_Ocean_Best_Estimate"],
        "aerosol_type": ["Aerosol_Type_Land_Ocean"],
    },
    "dt": {
        "lat": ["geolocation_data/latitude", "Latitude", "latitude"],
        "lon": ["geolocation_data/longitude", "Longitude", "longitude"],
        "time": ["geolocation_data/Scan_Start_Time", "Scan_Start_Time", "scan_start_time"],
        "aod550": [
            "geophysical_data/Optical_Depth_Land_And_Ocean",
            "Optical_Depth_Land_And_Ocean",
        ],
        "ae": ["geophysical_data/Angstrom_Exponent_1_Ocean", "Angstrom_Exponent_1_Ocean"],
        "aerosol_type": ["geophysical_data/Aerosol_Type_Land", "Aerosol_Type_Land"],
    },
}
REQUIRED = ("lat", "lon", "aod550")
GRANULE_RE = re.compile(r"(AER(?:DB|DT)_L2_VIIRS_[A-Z0-9]+)\.A(\d{4})(\d{3})\.(\d{2})(\d{2})")
TIME_UNITS_RE = re.compile(r"seconds since (\d{4}-\d{2}-\d{2})[ T]?(\d{2}:\d{2}:\d{2})?")
VALID_AOD = (0.0, 5.0)


def granule_info(path: str | os.PathLike) -> tuple[str, dt.datetime]:
    """(product short name, granule start UTC) from a standard VIIRS file name."""
    m = GRANULE_RE.search(Path(path).name)
    if not m:
        raise ValueError(f"not a VIIRS aerosol L2 granule name: {Path(path).name}")
    product, year, doy, hh, mm = m.groups()
    start = dt.datetime(int(year), 1, 1, int(hh), int(mm), tzinfo=dt.UTC) + dt.timedelta(
        days=int(doy) - 1
    )
    return product, start


def _read(h5, names: list[str]) -> tuple[np.ndarray | None, dict]:
    for name in names:
        if name in h5:
            d = h5[name]
            raw = d[()]
            attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in d.attrs.items()}
            x = np.asarray(raw, dtype=np.float64)
            for key in ("_FillValue", "missing_value"):
                if key in attrs:
                    x = np.where(raw == np.asarray(attrs[key]).ravel()[0], np.nan, x)
            if "valid_range" in attrs:
                lo, hi = np.asarray(attrs["valid_range"]).ravel()[:2]
                x = np.where((raw < lo) | (raw > hi), np.nan, x)
            scale = float(np.asarray(attrs.get("scale_factor", 1.0)).ravel()[0])
            offset = float(np.asarray(attrs.get("add_offset", 0.0)).ravel()[0])
            return x * scale + offset, attrs
    return None, {}


def _times(values: np.ndarray, attrs: dict, shape, start: dt.datetime) -> np.ndarray:
    """Pixel times (datetime64[us]); granule start if the file has no usable scan time."""
    fallback = np.datetime64(start.replace(tzinfo=None), "us")
    m = TIME_UNITS_RE.search(str(attrs.get("units", "")))
    if values is None or not m:
        return np.full(shape, fallback)
    v = values
    if v.shape != shape:
        if v.ndim == 1 and len(shape) == 2 and v.shape[0] == shape[0]:  # one time per scan line
            v = np.repeat(v[:, None], shape[1], axis=1)
        else:
            return np.full(shape, fallback)
    epoch = np.datetime64(f"{m.group(1)}T{m.group(2) or '00:00:00'}", "us")
    t = epoch + (np.nan_to_num(v, nan=0.0) * 1e6).astype("timedelta64[us]")
    return np.where(np.isfinite(v), t, fallback)


def read_granule(path: str | os.PathLike) -> pl.DataFrame:
    """Valid pixels of one Deep Blue / Dark Target granule (netCDF4/HDF5)."""
    import h5py

    product, start = granule_info(path)
    fields = FIELDS[PRODUCTS[product]]
    with h5py.File(path, "r") as h5:
        data, attrs = {}, {}
        for col, names in fields.items():
            data[col], attrs[col] = _read(h5, names)
    missing = [c for c in REQUIRED if data[c] is None]
    if missing:
        raise KeyError(f"{Path(path).name}: no dataset for {missing}; run pyro-viirs inspect")
    shape = data["aod550"].shape
    t = _times(data["time"], attrs["time"], shape, start)
    cols = {
        "time": t.ravel(),
        "lat": data["lat"].ravel(),
        "lon": data["lon"].ravel(),
        "aod550": data["aod550"].ravel(),
        "ae": (data["ae"] if data["ae"] is not None else np.full(shape, np.nan)).ravel(),
        "aerosol_type": (
            data["aerosol_type"] if data["aerosol_type"] is not None else np.full(shape, np.nan)
        ).ravel(),
    }
    lo, hi = VALID_AOD
    df = pl.DataFrame(cols).with_columns(
        pl.col("time").cast(pl.Datetime("us")).dt.replace_time_zone("UTC"),
        pl.col("lat", "lon", "aod550", "ae").cast(pl.Float32),
        pl.col("aerosol_type").cast(pl.Float32),
        pl.lit(product).alias("product"),
        pl.lit(product.rsplit("_", 1)[-1]).alias("platform"),
        pl.lit(Path(path).name).alias("granule"),
    )
    return df.filter(
        pl.col("aod550").is_finite()
        & pl.col("aod550").is_between(lo, hi)
        & pl.col("lat").is_finite()
        & pl.col("lon").is_finite()
    )


def inspect(path: str | os.PathLike) -> list[str]:
    """Dataset paths with shape and units in a granule."""
    import h5py

    out = []
    with h5py.File(path, "r") as h5:

        def visit(name, obj):
            if isinstance(obj, h5py.Dataset):
                units = obj.attrs.get("units", b"")
                units = units.decode() if isinstance(units, bytes) else units
                out.append(f"{name}  {obj.shape}  {units}")

        h5.visititems(visit)
    return out


def extract(granules: list[Path], out_dir: Path) -> list[Path]:
    """Granules -> ``<product>_<YYYY>_<MM>.parquet`` (valid pixels, sorted by time)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    groups: dict[tuple[str, int, int], list[Path]] = {}
    for g in granules:
        product, start = granule_info(g)
        groups.setdefault((product, start.year, start.month), []).append(g)
    written = []
    for (product, y, m), files in sorted(groups.items()):
        frames = []
        for f in sorted(files):
            try:
                frames.append(read_granule(f))
            except (OSError, KeyError) as exc:
                print(f"skip {f.name}: {exc}", file=sys.stderr)
        if not frames:
            continue
        path = out_dir / f"{product}_{y:04d}_{m:02d}.parquet"
        pl.concat(frames).sort("time").write_parquet(path, compression="zstd")
        written.append(path)
    return written


def load_pixels(root: Path, events: pl.DataFrame, products: list[str] | None = None):
    """Pixels from ``extract`` output covering the event windows."""
    files = sorted(root.glob("*.parquet"))
    if products:
        files = [f for f in files if any(f.name.startswith(p + "_") for p in products)]
    if not files:
        return pl.DataFrame()
    t0, t1 = events["window_start"].min(), events["window_end"].max()
    return pl.concat(
        pl.collect_all(
            [pl.scan_parquet(f).filter(pl.col("time").is_between(t0, t1)) for f in files]
        ),
        how="diagonal",
    )


VIIRS_VALUES = {"o_aod550": "aod550", "o_ae_native": "ae"}
EVENT_KEYS = ("pcb_id", "region", "kept", "product")


def match_event_viirs(
    events: pl.DataFrame,
    pixels: pl.DataFrame,
    src: ppe.Source,
    *,
    radius_km: float = 500.0,
    pre_hours: float = 24.0,
    max_hours_after: float = 240.0,
    agg: str = "mean",
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """VIIRS super-obs per (event, product, 3-hourly step, model cell) and all-member
    samples of ``src`` (3-hourly only), as ``pyro.match.match_event_model``."""
    from pyro.events import add_windows
    from pyro.match import sample_model

    if src.resolution != "3h":
        raise ValueError(f"{src.key} is {src.resolution}; events use 3-hourly output only")
    ev = add_windows(events, pre_hours / 24.0, max_hours_after / 24.0)
    near = gridding.pixels_near_events(ev, pixels, radius_km)
    if near.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    near = near.filter(pl.col("dt_hours").is_between(-pre_hours, max_hours_after))
    so = gridding.grid_model(near, src, VIIRS_VALUES, keys=EVENT_KEYS, agg=agg)
    if so.is_empty():
        return so, pl.DataFrame()
    so = so.with_columns(pl.lit(None, dtype=pl.Float32).alias("o_aod550_unc"))
    model = sample_model(so, src, ["od550aer", "ae440_670", "ae550_865"])
    return so, model


# ---------------------------------------------------------------------- fetch


def fetch(
    events: pl.DataFrame,
    products: list[str],
    out_dir: Path,
    *,
    radius_km: float = 1000.0,
    pre_hours: float = 24.0,
    max_hours_after: float = 240.0,
) -> list[Path]:
    """Download the granules intersecting each event's window and radius box."""
    import earthaccess

    earthaccess.login(strategy="environment")
    got: list[Path] = []
    for ev in events.iter_rows(named=True):
        t0 = ev["onset_utc"] - dt.timedelta(hours=pre_hours)
        t1 = ev["onset_utc"] + dt.timedelta(hours=max_hours_after)
        dlat = radius_km / 111.0
        dlon = min(radius_km / (111.0 * max(np.cos(np.radians(ev["lat_c"])), 0.05)), 179.0)
        bbox = (
            max(ev["lon_c"] - dlon, -180.0),
            max(ev["lat_c"] - dlat, -90.0),
            min(ev["lon_c"] + dlon, 180.0),
            min(ev["lat_c"] + dlat, 90.0),
        )
        for product in products:
            results = earthaccess.search_data(
                short_name=product,
                temporal=(t0.strftime("%Y-%m-%dT%H:%M:%S"), t1.strftime("%Y-%m-%dT%H:%M:%S")),
                bounding_box=bbox,
            )
            todo = []
            for r in results:
                name = Path(r.data_links()[0]).name
                _, start = granule_info(name)
                d = out_dir / product / f"{start:%Y}" / f"{start:%j}"
                if not (d / name).exists():
                    todo.append((r, d))
                got.append(d / name)
            for r, d in todo:
                d.mkdir(parents=True, exist_ok=True)
                earthaccess.download([r], local_path=str(d))
    return sorted(set(got))


# ------------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    from pyro.envfile import load_env_file
    from pyro.events import load_events

    p = argparse.ArgumentParser(prog="pyro-viirs", description=__doc__.splitlines()[0])
    p.add_argument(
        "--env-file", default=str(Path(__file__).resolve().parents[2] / "config" / "local.env")
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download granules around the events")
    f.add_argument("events", type=Path)
    f.add_argument("--product", action="append", default=None, choices=sorted(PRODUCTS))
    f.add_argument(
        "--raw", type=Path, default=None, help="default $PYRO_DATA_ROOT/raw/satellite/viirs"
    )
    f.add_argument("--radius-km", type=float, default=1000.0)
    f.add_argument("--pre-hours", type=float, default=24.0)
    f.add_argument("--max-hours-after", type=float, default=240.0)
    e = sub.add_parser("extract", help="granules -> monthly pixel parquet")
    e.add_argument("--raw", type=Path, default=None)
    e.add_argument(
        "--out", type=Path, default=None, help="default $PYRO_DATA_ROOT/raw/satellite/viirs_pixels"
    )
    i = sub.add_parser("inspect", help="list the datasets in a granule")
    i.add_argument("granule", type=Path)
    g = sub.add_parser("grid", help="event super-obs on the 3-hourly PPE grid + member samples")
    g.add_argument("events", type=Path)
    g.add_argument("--pixels", type=Path, default=None)
    g.add_argument(
        "--out", type=Path, default=None, help="default $PYRO_DATA_ROOT/derived/viirs_event_match"
    )
    g.add_argument("--ppe", default="ybhatti_pd_3h")
    g.add_argument("--product", action="append", default=None, choices=sorted(PRODUCTS))
    g.add_argument("--radius-km", type=float, default=500.0)
    g.add_argument("--pre-hours", type=float, default=24.0)
    g.add_argument("--max-hours-after", type=float, default=240.0)
    a = p.parse_args(argv)
    if Path(a.env_file).exists():
        load_env_file(a.env_file)
    root = Path(os.environ.get("PYRO_DATA_ROOT", "data"))
    raw = getattr(a, "raw", None) or root / "raw" / "satellite" / "viirs"

    if a.cmd == "inspect":
        print("\n".join(inspect(a.granule)))
    elif a.cmd == "fetch":
        files = fetch(
            load_events(a.events),
            a.product or ["AERDB_L2_VIIRS_SNPP"],
            raw,
            radius_km=a.radius_km,
            pre_hours=a.pre_hours,
            max_hours_after=a.max_hours_after,
        )
        print(f"{len(files)} granules under {raw}")
    elif a.cmd == "extract":
        out = a.out or root / "raw" / "satellite" / "viirs_pixels"
        written = extract(sorted(raw.rglob("AER*_L2_VIIRS_*.nc")), out)
        print(f"{len(written)} monthly files -> {out}")
    elif a.cmd == "grid":
        from pyro.events import add_windows

        pix_dir = a.pixels or root / "raw" / "satellite" / "viirs_pixels"
        out = a.out or root / "derived" / "viirs_event_match"
        out.mkdir(parents=True, exist_ok=True)
        events = load_events(a.events)
        pixels = load_pixels(
            pix_dir, add_windows(events, a.pre_hours / 24, a.max_hours_after / 24), a.product
        )
        src = ppe.open_source(a.ppe)
        so, model = match_event_viirs(
            events,
            pixels,
            src,
            radius_km=a.radius_km,
            pre_hours=a.pre_hours,
            max_hours_after=a.max_hours_after,
        )
        if so.is_empty():
            print("no VIIRS super-obs")
            return 0
        so.write_parquet(out / f"superobs_{a.ppe}.parquet", compression="zstd")
        model.write_parquet(out / f"model_{a.ppe}.parquet", compression="zstd")
        print(f"{so.height} super-obs, {so['pcb_id'].n_unique()} events -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
