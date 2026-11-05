"""Convert the ICON-HAM PD PPE (2024-25, monthly) per-member netCDF files to parquet.

Source layout (one directory per variable, one file per member)::

    <src>/<var>/ens_<NNN>_PD_<var>.nc      time x ncells (R02B04, 20480 cells)

Output layout (hive-partitioned, one wide table per time step)::

    <out>/values/time_label=YYYY-MM-DD/part-0.parquet
        member u16, cell u32, lat f32, lon f32, <var> f32 ...
    <out>/grid.parquet         cell, lat, lon, 3 vertex lat/lon (degrees)
    <out>/members.parquet      member x variable availability
    <out>/metadata.json        units, long names, provenance, float32 error

Rows are sorted by latitude, then cell, then member, so parquet row-group
statistics let ``lat``/``lon`` filters skip most of a file. Values are stored
as float32 (byte-stream-split + zstd); the maximum relative rounding error is
recorded per variable in ``metadata.json``.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

FILE_RE = re.compile(r"^ens_(\d{3})_PD_(.+)\.nc$")
ROW_GROUP_SIZE = 262_144
ZSTD_LEVEL = 9


def discover(src: Path) -> dict[str, dict[int, Path]]:
    """Map variable -> {member: file} for every variable directory under ``src``."""
    out: dict[str, dict[int, Path]] = {}
    for d in sorted(p for p in src.iterdir() if p.is_dir()):
        files: dict[int, Path] = {}
        for f in d.iterdir():
            m = FILE_RE.match(f.name)
            if m and m.group(2) == d.name:
                files[int(m.group(1))] = f
        if files:
            out[d.name] = files
    return out


def _decode_time(values: np.ndarray, units: str) -> list[str]:
    if not units.startswith("day as %Y%m%d"):
        raise ValueError(f"unsupported time units {units!r}")
    labels = []
    for v in values:
        day = int(v)
        frac = float(v) - day
        if abs(frac) > 1e-6:
            raise ValueError(f"sub-daily time label {v} not supported for monthly data")
        labels.append(datetime.strptime(str(day), "%Y%m%d").strftime("%Y-%m-%d"))
    return labels


def read_grid(path: Path) -> dict[str, np.ndarray]:
    import netCDF4

    with netCDF4.Dataset(path) as ds:
        g = {k: np.degrees(np.asarray(ds[k][:], dtype=np.float64)) for k in ("clat", "clon")}
        g["clat_bnds"] = np.degrees(np.asarray(ds["clat_bnds"][:], dtype=np.float64))
        g["clon_bnds"] = np.degrees(np.asarray(ds["clon_bnds"][:], dtype=np.float64))
        g["time"] = np.asarray(ds["time"][:])
        g["time_units"] = ds["time"].units
    return g


def _load_variable(args: tuple) -> dict:
    """Read one variable for all members into slot ``vi`` of the shared memmap."""
    import netCDF4

    var, vi, files, members, mm_path, shape, ref_time, ref_lat = args
    cube = np.memmap(mm_path, dtype=np.float32, mode="r+", shape=shape)
    max_rel = 0.0
    max_abs = 0.0
    n_nan = 0
    attrs: dict = {}
    for mi, mem in enumerate(members):
        f = files.get(mem)
        if f is None:
            cube[vi, mi] = np.nan
            continue
        with netCDF4.Dataset(f) as ds:
            v = ds[var]
            if not attrs:
                attrs = {k: str(v.getncattr(k)) for k in ("long_name", "units") if k in v.ncattrs()}
            t = np.asarray(ds["time"][:])
            if t.shape != ref_time.shape or not np.array_equal(t, ref_time):
                raise ValueError(f"{f}: time axis differs from reference")
            if not np.allclose(np.asarray(ds["clat"][:]), ref_lat):
                raise ValueError(f"{f}: grid differs from reference")
            x = np.ma.filled(v[:].astype(np.float64), np.nan)
        x32 = x.astype(np.float32)
        nz = np.isfinite(x) & (x != 0)
        if nz.any():
            err = np.abs(x32[nz].astype(np.float64) - x[nz])
            max_rel = max(max_rel, float((err / np.abs(x[nz])).max()))
            max_abs = max(max_abs, float(err.max()))
        n_nan += int(np.isnan(x).sum())
        cube[vi, mi] = x32
    cube.flush()
    return {
        "var": var,
        "attrs": attrs,
        "max_rel_err_float32": max_rel,
        "max_abs_err_float32": max_abs,
        "n_nan": n_nan,
    }


def convert(
    src: Path,
    out: Path,
    workdir: Path,
    variables: list[str] | None = None,
    workers: int = 8,
) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    t0 = time.time()
    found = discover(src)
    if variables:
        unknown = sorted(set(variables) - set(found))
        if unknown:
            raise SystemExit(f"variables not found under {src}: {unknown}")
        found = {v: found[v] for v in variables}
    var_names = sorted(found)
    members = sorted(set().union(*[set(f) for f in found.values()]))
    first = next(iter(found[var_names[0]].values()))
    grid = read_grid(first)
    labels = _decode_time(grid["time"], grid["time_units"])
    nt, nv, nm, nc = len(labels), len(var_names), len(members), grid["clat"].size
    # (variable, member) blocks are contiguous so each source file is one sequential write.
    shape = (nv, nm, nt, nc)

    workdir.mkdir(parents=True, exist_ok=True)
    mm_path = workdir / "icon_ppe_cube.f32"
    np.memmap(mm_path, dtype=np.float32, mode="w+", shape=shape).flush()
    ref_lat = np.radians(grid["clat"])
    jobs = [
        (v, i, found[v], members, str(mm_path), shape, grid["time"], ref_lat)
        for i, v in enumerate(var_names)
    ]
    var_meta: dict[str, dict] = {}
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for res in ex.map(_load_variable, jobs):
            var_meta[res["var"]] = res
            print(f"[{time.time() - t0:7.0f}s] read {res['var']}", flush=True)

    order = np.lexsort((np.arange(nc), grid["clat"]))
    lat32 = grid["clat"].astype(np.float32)[order]
    lon32 = grid["clon"].astype(np.float32)[order]
    cell_col = np.repeat(order.astype(np.uint32), nm)
    lat_col = np.repeat(lat32, nm)
    lon_col = np.repeat(lon32, nm)
    member_col = np.tile(np.asarray(members, dtype=np.uint16), nc)

    cube = np.memmap(mm_path, dtype=np.float32, mode="r", shape=shape)
    values_dir = out / "values"
    for ti, label in enumerate(labels):
        cols = {"member": member_col, "cell": cell_col, "lat": lat_col, "lon": lon_col}
        for vi, v in enumerate(var_names):
            cols[v] = np.ascontiguousarray(cube[vi, :, ti][:, order].T).reshape(-1)
        table = pa.table(cols)
        part = values_dir / f"time_label={label}"
        part.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            table,
            part / "part-0.parquet",
            compression="zstd",
            compression_level=ZSTD_LEVEL,
            row_group_size=ROW_GROUP_SIZE,
            use_dictionary=["member", "cell", "lat", "lon"],
            use_byte_stream_split=var_names,
            write_statistics=True,
        )
        print(f"[{time.time() - t0:7.0f}s] wrote {label}", flush=True)
    del cube
    mm_path.unlink()

    grid_table = pa.table(
        {
            "cell": np.arange(nc, dtype=np.uint32),
            "lat": grid["clat"],
            "lon": grid["clon"],
            **{f"vlat{k}": grid["clat_bnds"][:, k] for k in range(3)},
            **{f"vlon{k}": grid["clon_bnds"][:, k] for k in range(3)},
        }
    )
    pq.write_table(grid_table, out / "grid.parquet", compression="zstd")
    avail = {"member": np.asarray(members, dtype=np.uint16)}
    for v in var_names:
        avail[v] = np.asarray([m in found[v] for m in members])
    pq.write_table(pa.table(avail), out / "members.parquet", compression="zstd")

    meta = {
        "dataset": "ICON-HAM PD PPE 2024-25 (monthly means)",
        "source": str(src),
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grid": "ICON R02B04 (icon_grid_0005_R02B04_G), 20480 cells",
        "n_members": nm,
        "members_missing": sorted(set(range(min(members), max(members) + 1)) - set(members)),
        "time_labels": labels,
        "time_label_note": (
            "Raw ICON time stamps ('day as %Y%m%d.%f'). ICON mean output is usually stamped "
            "at the END of the averaging interval, so 2024-06-01 may be the May-2024 mean; "
            "confirm with the data provider before using month-specific values."
        ),
        "float32_note": (
            "Relative rounding error is ~6e-8 for normal float32 values; it is larger only "
            "for |x| < 1.18e-38 (subnormal range, e.g. tiny deposition fluxes), where the "
            "absolute error stays below max_abs_err_float32."
        ),
        "storage": {
            "dtype": "float32",
            "compression": f"zstd-{ZSTD_LEVEL}",
            "row_group_size": ROW_GROUP_SIZE,
            "sort": ["lat", "cell", "member"],
        },
        "variables": {
            v: {
                k: var_meta[v][k]
                for k in ("attrs", "max_rel_err_float32", "max_abs_err_float32", "n_nan")
            }
            for v in var_names
        },
        "elapsed_s": round(time.time() - t0, 1),
    }
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def verify(src: Path, out: Path, n_samples: int = 20, seed: int = 0) -> int:
    """Compare random (variable, member) slices of the parquet store to the source files."""
    import netCDF4
    import polars as pl

    meta = json.loads((out / "metadata.json").read_text())
    labels = meta["time_labels"]
    found = discover(src)
    rng = np.random.default_rng(seed)
    lf = pl.scan_parquet(out / "values" / "**" / "*.parquet", hive_partitioning=True)
    bad = 0
    for _ in range(n_samples):
        v = str(rng.choice(sorted(meta["variables"])))
        mem = int(rng.choice(sorted(found[v])))
        with netCDF4.Dataset(found[v][mem]) as ds:
            ref = np.ma.filled(ds[v][:].astype(np.float64), np.nan).astype(np.float32)
        got = (
            lf.filter(pl.col("member") == mem)
            .select("time_label", "cell", v)
            .collect()
            .sort("time_label", "cell")
        )
        arr = got[v].to_numpy().reshape(len(labels), -1)
        ok = np.array_equal(arr, ref, equal_nan=True)
        bad += not ok
        print(f"{'OK ' if ok else 'BAD'} {v} member {mem:03d}")
    return bad


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pyro-icon", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="netCDF -> parquet store")
    c.add_argument("--src", type=Path, default=os.environ.get("ICON_PPE_SOURCE_ROOT"))
    c.add_argument("--out", type=Path, default=os.environ.get("ICON_PPE_STORE"))
    c.add_argument("--workdir", type=Path, default=os.environ.get("TMPDIR", "/tmp"))
    c.add_argument("--var", action="append", dest="variables")
    c.add_argument("--workers", type=int, default=8)
    v = sub.add_parser("verify", help="spot-check the store against the source")
    v.add_argument("--src", type=Path, default=os.environ.get("ICON_PPE_SOURCE_ROOT"))
    v.add_argument("--out", type=Path, default=os.environ.get("ICON_PPE_STORE"))
    v.add_argument("-n", type=int, default=20)
    a = p.parse_args(argv)
    if a.src is None or a.out is None:
        p.error("--src/--out (or ICON_PPE_SOURCE_ROOT/ICON_PPE_STORE) are required")
    if a.cmd == "convert":
        meta = convert(a.src, a.out, a.workdir, a.variables, a.workers)
        print(json.dumps({k: meta[k] for k in ("n_members", "time_labels", "elapsed_s")}))
        return 0
    return 1 if verify(a.src, a.out, a.n) else 0


if __name__ == "__main__":
    sys.exit(main())
