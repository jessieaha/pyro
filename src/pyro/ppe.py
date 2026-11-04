"""Common reader layer for the PPE datasets (ECHAM-HAM by ybhatti, ICON-HAM by hjia).

Every source is described in ``config/ppe_sources.toml`` and exposes the same API:

    src = open_source("ybhatti_pd_3h")
    df = sample(src, points, ["od550aer", "ssa550aer"])   # long table, common schema

``points`` is a polars DataFrame with ``point_id``, ``time`` (UTC datetime), ``lat``
and ``lon`` (degrees). The result has one row per (point, member)::

    point_id, ppe_source, dataset, model, resolution, member, is_control,
    t_idx, model_time, dt_hours, cell, model_lat, model_lon, <canonical variables>

Conventions shared by all sources: canonical AeroCom-style variable names and
units (``VARIABLES``), longitudes in [-180, 180), UTC times. ``model_time`` is the
instant of instantaneous output or the midpoint of a mean interval; ``dt_hours``
is the observation time minus ``model_time``. Points outside a source's period
get ``t_idx = -1`` and null values. Model fields are never interpolated:
the nearest step (or the containing mean interval) and the nearest cell are used.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from pyro.envfile import expand

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "ppe_sources.toml"
PPE_SOURCES = ("ybhatti", "hjia")


@dataclass(frozen=True)
class Variable:
    long_name: str
    units: str
    echam: str | None = None
    icon: str | None = None


_TAU = "TAU_COMP_{}_550nm"

# Canonical name -> native names. Both models write the HAM radiation diagnostics
# under the same names and units. ``None`` = not in that model's sub-daily output
# (ECHAM component AODs exist only in the daily rad stream; ICON wrote no burdens,
# ECHAM no emission/deposition at sub-monthly frequency).
VARIABLES: dict[str, Variable] = {
    "od550aer": Variable("AOD at 550 nm", "1", "TAU_2D_550nm", "TAU_2D_550nm"),
    "abs550aer": Variable("absorption AOD at 550 nm", "1", "ABS_2D_550nm", "ABS_2D_550nm"),
    "od440aer": Variable("AOD at 440 nm", "1", None, "TAU_2D_440nm"),
    "abs440aer": Variable("absorption AOD at 440 nm", "1", None, "ABS_2D_440nm"),
    "ae440_670": Variable("Angstrom exponent 440-670 nm", "1", *["ANG_440nm_670nm"] * 2),
    "ae550_865": Variable("Angstrom exponent 550-865 nm", "1", *["ANG_550nm_865nm"] * 2),
    "od550bc": Variable("BC AOD at 550 nm", "1", None, _TAU.format("BC")),
    "od550oa": Variable("OC AOD at 550 nm", "1", None, _TAU.format("OC")),
    "od550so4": Variable("SO4 AOD at 550 nm", "1", None, _TAU.format("SO4")),
    "od550ss": Variable("sea-salt AOD at 550 nm", "1", None, _TAU.format("SS")),
    "od550dust": Variable("dust AOD at 550 nm", "1", None, _TAU.format("DU")),
    "od550aerh2o": Variable("aerosol water AOD at 550 nm", "1", None, _TAU.format("WAT")),
    "loadbc": Variable("BC burden", "kg m-2", "burden_BC"),
    "loadoa": Variable("OC burden", "kg m-2", "burden_OC"),
    "loadso4": Variable("SO4 burden", "kg m-2", "burden_SO4"),
    "loadss": Variable("sea-salt burden", "kg m-2", "burden_SS"),
    "loaddust": Variable("dust burden", "kg m-2", "burden_DU"),
    "emibc": Variable("BC emission flux", "kg m-2 s-1", None, "emi_bc"),
    "emioa": Variable("OC emission flux", "kg m-2 s-1", None, "emi_oc"),
    "emiso2": Variable("SO2 emission flux", "kg m-2 s-1", None, "emi_so2"),
    "emiss": Variable("sea-salt emission flux", "kg m-2 s-1", None, "emi_ss"),
    "emidust": Variable("dust emission flux", "kg m-2 s-1", None, "emi_du"),
    "drybc": Variable("BC dry deposition flux", "kg m-2 s-1", None, "ddep_bc"),
    "dryoa": Variable("OC dry deposition flux", "kg m-2 s-1", None, "ddep_oc"),
    "dryso4": Variable("SO4 dry deposition flux", "kg m-2 s-1", None, "ddep_so4"),
    "dryss": Variable("sea-salt dry deposition flux", "kg m-2 s-1", None, "ddep_ss"),
    "drydust": Variable("dust dry deposition flux", "kg m-2 s-1", None, "ddep_du"),
    "pr": Variable("precipitation flux", "kg m-2 s-1", None, "pr"),
}
# Derived, identically for every source: SSA = 1 - AAOD / AOD.
DERIVED = {"ssa550aer": ("abs550aer", "od550aer"), "ssa440aer": ("abs440aer", "od440aer")}

COMMON_COLUMNS: dict[str, pl.DataType] = {
    "point_id": pl.Int64,
    "ppe_source": pl.String,
    "dataset": pl.String,
    "model": pl.String,
    "resolution": pl.String,
    "member": pl.Int32,
    "is_control": pl.Boolean,
    "t_idx": pl.Int32,
    "model_time": pl.Datetime("us", "UTC"),
    "dt_hours": pl.Float64,
    "cell": pl.Int64,
    "model_lat": pl.Float32,
    "model_lon": pl.Float32,
}


# --------------------------------------------------------------------------- time


def _to_datetime64(values: np.ndarray, units: str) -> np.ndarray:
    """Decode CF ``days since`` or ICON/CDO ``day as %Y%m%d.%f`` to datetime64[s] (UTC)."""
    v = np.asarray(values, dtype=np.float64)
    if units.startswith("day as %Y%m%d"):
        day = np.floor(v).astype(np.int64)
        base = np.array(
            [np.datetime64(f"{d // 10000:04d}-{d // 100 % 100:02d}-{d % 100:02d}") for d in day],
            dtype="datetime64[s]",
        )
        return base + np.rint((v - day) * 86400).astype("timedelta64[s]")
    m = re.match(r"(days|hours) since (\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", units)
    if not m:
        raise ValueError(f"unsupported time units {units!r}")
    unit, y, mo, d, hh, mm = m.groups()
    t0 = np.datetime64(
        f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{int(hh or 0):02d}:{int(mm or 0):02d}", "s"
    )
    scale = 86400 if unit == "days" else 3600
    return t0 + np.rint(v * scale).astype("timedelta64[s]")


def _add_months(t: np.datetime64, n: int) -> np.datetime64:
    m = t.astype("datetime64[M]") + np.timedelta64(n, "M")
    return m.astype("datetime64[s]") + (t - t.astype("datetime64[M]").astype("datetime64[s]"))


@dataclass
class TimeAxis:
    """Model time axis. ``kind`` is ``instant`` or ``interval`` (time means)."""

    stamps: np.ndarray  # datetime64[s], as written in the files
    kind: str = "instant"
    label: str = "end"  # interval only: stamp marks the 'end' or 'start' of the mean
    step: str = ""  # interval only: '1mon' or e.g. '6h'
    starts: np.ndarray = field(init=False)
    ends: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        s = np.asarray(self.stamps, dtype="datetime64[s]")
        self.stamps = s
        if self.kind == "instant":
            self.starts = self.ends = s
            return
        if self.kind != "interval":
            raise ValueError(f"time kind must be instant or interval, not {self.kind!r}")
        if self.step == "1mon":
            shift = [
                (_add_months(t, -1), t) if self.label == "end" else (t, _add_months(t, 1))
                for t in s
            ]
            self.starts = np.array([a for a, _ in shift], dtype="datetime64[s]")
            self.ends = np.array([b for _, b in shift], dtype="datetime64[s]")
        else:
            dt = np.timedelta64(_step_seconds(self.step), "s")
            self.starts, self.ends = (s - dt, s) if self.label == "end" else (s, s + dt)

    @property
    def model_time(self) -> np.ndarray:
        return self.starts + (self.ends - self.starts) // 2

    def locate(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Index of the nearest instant / containing interval (-1 outside) and dt in hours."""
        t = np.asarray(t, dtype="datetime64[s]")
        if self.kind == "instant":
            s = self.stamps
            i = (
                np.clip(np.searchsorted(s, t), 1, len(s) - 1)
                if len(s) > 1
                else np.zeros(len(t), dtype=np.int64)
            )
            if len(s) > 1:
                left = np.abs(t - s[i - 1]) <= np.abs(s[i] - t)
                i = np.where(left, i - 1, i)
            half = (np.median(np.diff(s)) // 2) if len(s) > 1 else np.timedelta64(0, "s")
            ok = np.abs(t - s[i]) <= half
        else:
            i = np.searchsorted(self.ends, t, side="right")
            i = np.clip(i, 0, len(self.ends) - 1)
            ok = (t >= self.starts[i]) & (t < self.ends[i])
        idx = np.where(ok, i, -1).astype(np.int64)
        mt = self.model_time[np.where(ok, i, 0)]
        dt = np.where(ok, (t - mt).astype("timedelta64[s]").astype(np.float64) / 3600, np.nan)
        return idx, dt


def _step_seconds(step: str) -> int:
    m = re.fullmatch(r"(\d+)h", step)
    if not m:
        raise ValueError(f"unsupported step {step!r}")
    return int(m.group(1)) * 3600


# --------------------------------------------------------------------------- grids


def _wrap180(lon: np.ndarray) -> np.ndarray:
    return (np.asarray(lon, dtype=np.float64) + 180.0) % 360.0 - 180.0


class GaussianGrid:
    """Regular-longitude Gaussian grid (ECHAM T63: 96 x 192); cell = j * nlon + i."""

    name = "T63"

    def __init__(self, lat: np.ndarray, lon: np.ndarray):
        self.lat = np.asarray(lat, dtype=np.float64)
        self.lon = np.asarray(lon, dtype=np.float64)
        self.dlon = float(self.lon[1] - self.lon[0])

    def locate(self, lat, lon) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        lat = np.asarray(lat, dtype=np.float64)
        i = np.rint(((np.asarray(lon) - self.lon[0]) % 360.0) / self.dlon).astype(np.int64)
        i %= self.lon.size
        j = np.abs(lat[:, None] - self.lat[None, :]).argmin(axis=1)
        return j * self.lon.size + i, self.lat[j], _wrap180(self.lon[i])

    def split(self, cell: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return np.asarray(cell) // self.lon.size, np.asarray(cell) % self.lon.size


class UnstructuredGrid:
    """ICON triangular grid; nearest cell centre by great-circle distance."""

    name = "R02B04"

    def __init__(self, lat_deg: np.ndarray, lon_deg: np.ndarray):
        self.lat = np.asarray(lat_deg, dtype=np.float64)
        self.lon = _wrap180(lon_deg)
        self._xyz = _xyz(self.lat, self.lon)
        self._tree = None

    def locate(self, lat, lon) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        q = _xyz(np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64))
        try:
            from scipy.spatial import cKDTree

            if self._tree is None:
                self._tree = cKDTree(self._xyz)
            cell = self._tree.query(q)[1].astype(np.int64)
        except ImportError:  # pragma: no cover - scipy is in the analysis extra
            cell = np.concatenate(
                [(c @ self._xyz.T).argmax(axis=1) for c in np.array_split(q, max(1, len(q) // 512))]
            )
        return cell, self.lat[cell], self.lon[cell]


def _xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=-1)


# --------------------------------------------------------------------------- tar access


class _TarSlice(io.RawIOBase):
    """Read-only file object for one member stored uncompressed inside a tar file."""

    def __init__(self, path: Path, offset: int, size: int):
        self._f = open(path, "rb")  # noqa: SIM115 - closed in close()
        self._off, self._size, self._pos = offset, size, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, pos: int, whence: int = 0) -> int:
        base = {0: 0, 1: self._pos, 2: self._size}[whence]
        self._pos = base + pos
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, b) -> int:
        n = min(len(b), self._size - self._pos)
        if n <= 0:
            return 0
        self._f.seek(self._off + self._pos)
        data = self._f.read(n)
        b[: len(data)] = data
        self._pos += len(data)
        return len(data)

    def close(self) -> None:
        self._f.close()
        super().close()


def tar_index(tar: Path) -> dict[str, tuple[int, int]]:
    """Member name -> (data offset, size). Uses the dmftar ``.idx`` listing if present
    (``tar -tv`` format, avoids scanning a large tar); falls back to reading headers."""
    idx = Path(str(tar) + ".idx")
    out: dict[str, tuple[int, int]] = {}
    if idx.exists():
        pos = 0
        for line in idx.read_text().splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            size, name = int(parts[2]), parts[5]
            if len(name.encode()) > 99:  # GNU long-name entries add headers; use tarfile
                out = {}
                break
            pos += 512
            if not parts[0].startswith("d"):
                out[name] = (pos, size)
            pos += -(-size // 512) * 512
        if out:
            return out
    with tarfile.open(tar, "r:") as tf:
        return {m.name: (m.offset_data, m.size) for m in tf if m.isfile()}


def _check_header(tar: Path, name: str, offset: int) -> None:
    with open(tar, "rb") as f:
        f.seek(offset - 512)
        hdr = tarfile.TarInfo.frombuf(f.read(512), "utf-8", "surrogateescape")
    if hdr.name != name:
        raise ValueError(f"{tar}: index points to {hdr.name!r}, expected {name!r}")


# --------------------------------------------------------------------------- sources


class Source:
    """Base class; subclasses set ``grid``, ``time``, ``members`` and implement ``read``."""

    native_kind = ""
    grid: Any
    time: TimeAxis
    members: np.ndarray
    control_member: int | None = None

    def __init__(
        self, key: str, ppe_source: str, model: str, resolution: str, description: str = ""
    ):
        if ppe_source not in PPE_SOURCES:
            raise ValueError(f"ppe_source must be one of {PPE_SOURCES}, not {ppe_source!r}")
        self.key, self.ppe_source, self.model = key, ppe_source, model
        self.resolution, self.description = resolution, description

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.key!r}, ppe_source={self.ppe_source!r})"

    def native(self, var: str) -> str:
        v = VARIABLES[var]
        name = getattr(v, self.native_kind)
        if name is None:
            raise KeyError(f"{var} is not available from {self.key}")
        return name

    def available(self) -> list[str]:
        return [k for k, v in VARIABLES.items() if getattr(v, self.native_kind)]

    def read(
        self, var: str, t_idx: np.ndarray, cell: np.ndarray, members: np.ndarray
    ) -> np.ndarray:
        """Values (n_members, n_points) at valid indices (t_idx >= 0)."""
        raise NotImplementedError


class EchamStacked(Source):
    """ybhatti ECHAM-HAM PD PPE: ensemble-stacked ``<VAR>_hifreq_PPE.nc``
    (ensemble, time, lat, lon); index 0 = Control, i = PPE_ENS_i. Variables that are
    not stacked are read from the per-member ``*_hifreq.nc`` files under ``member_root``."""

    native_kind = "echam"
    STACKED_VAR = {"SSA_2D_550nm": "__xarray_dataarray_variable__"}

    def __init__(
        self,
        key,
        ppe_source,
        model,
        resolution,
        root,
        member_root=None,
        time_kind="instant",
        description="",
    ):
        super().__init__(key, ppe_source, model, resolution, description)
        self.native_kind = "echam"
        self.root = Path(root)
        self.member_root = Path(member_root) if member_root else None
        import netCDF4

        ref = self.root / "TAU_2D_550nm_hifreq_PPE.nc"
        if not ref.exists():
            raise FileNotFoundError(f"{ref} not found")
        with netCDF4.Dataset(ref) as ds:
            self.grid = GaussianGrid(ds["lat"][:], ds["lon"][:])
            self.time = TimeAxis(_to_datetime64(ds["time"][:], ds["time"].units), time_kind)
            self.members = np.asarray(ds["ensemble"][:], dtype=np.int64)
        self.control_member = 0

    def _member_file(self, m: int, native: str) -> Path:
        name = "PPE_ENS_Control" if m == 0 else f"PPE_ENS_{m}"
        d = self.member_root / name
        extract = d / f"{native}_hifreq.nc"
        if extract.exists():
            return extract
        files = sorted(d.glob(f"{name}_*.01_hifreq.nc"))
        if not files:
            raise FileNotFoundError(f"no hifreq file for member {name}")
        return files[0]

    def available(self) -> list[str]:
        if self.member_root is not None:
            return super().available()
        return [
            k
            for k in super().available()
            if (self.root / f"{self.native(k)}_hifreq_PPE.nc").exists()
        ]

    def read(self, var, t_idx, cell, members):
        import netCDF4

        native = self.native(var)
        j, i = self.grid.split(cell)
        ut, inv = np.unique(t_idx, return_inverse=True)
        out = np.full((len(members), len(t_idx)), np.nan, dtype=np.float32)
        stacked = self.root / f"{native}_hifreq_PPE.nc"
        if stacked.exists():
            with netCDF4.Dataset(stacked) as ds:
                v = ds[self.STACKED_VAR.get(native, native)]
                v.set_auto_mask(False)
                pos = {int(m): k for k, m in enumerate(np.asarray(ds["ensemble"][:]))}
                for r, m in enumerate(members):
                    if int(m) in pos:
                        out[r] = np.asarray(v[pos[int(m)], ut])[inv, j, i]
            return out
        if self.member_root is None:
            raise FileNotFoundError(f"{stacked} missing and no member_root configured")
        for r, m in enumerate(members):
            path = self._member_file(int(m), native)
            with netCDF4.Dataset(path) as ds:
                t = _to_datetime64(ds["time"][:], ds["time"].units)
                if not np.array_equal(t, self.time.stamps):
                    raise ValueError(f"{path}: time axis differs from the stacked files")
                v = ds[native]
                v.set_auto_mask(False)
                out[r] = np.asarray(v[ut])[inv, j, i]
        return out


ICON_FILE_RE = re.compile(r"ens_(\d{3})_(PD|PI)_(.+)\.nc$")


class IconPerVariable(Source):
    """hjia ICON-HAM PPE: one directory per variable, one file per member
    ``<root>/<VAR>/ens_NNN_PD_<VAR>.nc`` (time, ncells), or the same tree packed in
    dmftar archives ``<root>/<VAR>.dmftar/0000/<VAR>.dmftar.tar`` (read in place)."""

    native_kind = "icon"

    def __init__(
        self,
        key,
        ppe_source,
        model,
        resolution,
        root,
        archive=False,
        time_kind="instant",
        time_label="end",
        time_step="",
        experiment="PD",
        control_member=None,
        description="",
    ):
        super().__init__(key, ppe_source, model, resolution, description)
        self.native_kind = "icon"
        self.root = Path(root)
        self.archive = bool(archive)
        self.experiment = experiment
        self._index: dict[str, dict[int, Any]] = {}
        probe_var = next(n for n in ("TAU_2D_550nm", "ABS_2D_550nm") if self._has(n))
        files = self._files(probe_var)
        self.members = np.array(sorted(files), dtype=np.int64)
        with self._open(probe_var, int(self.members[0])) as h:
            units = _attr(h["time"], "units")
            self.time = TimeAxis(
                _to_datetime64(h["time"][:], units), time_kind, time_label, time_step
            )
            self.grid = UnstructuredGrid(np.degrees(h["clat"][:]), np.degrees(h["clon"][:]))
        self.control_member = control_member

    def _tar(self, native: str) -> Path:
        return self.root / f"{native}.dmftar" / "0000" / f"{native}.dmftar.tar"

    def _has(self, native: str) -> bool:
        return self._tar(native).exists() if self.archive else (self.root / native).is_dir()

    def _files(self, native: str) -> dict[int, Any]:
        if native in self._index:
            return self._index[native]
        out: dict[int, Any] = {}
        if self.archive:
            tar = self._tar(native)
            for name, loc in tar_index(tar).items():
                m = ICON_FILE_RE.search(name)
                if m and m.group(2) == self.experiment and m.group(3) == native:
                    out.setdefault(int(m.group(1)), (tar, name, *loc))
        else:
            for f in (self.root / native).glob(f"ens_*_{self.experiment}_{native}.nc"):
                m = ICON_FILE_RE.search(f.name)
                if m:
                    out[int(m.group(1))] = f
        if not out:
            raise FileNotFoundError(f"no member files for {native} under {self.root}")
        self._index[native] = out
        return out

    def _open(self, native: str, member: int):
        import h5py

        loc = self._files(native)[member]
        if self.archive:
            tar, name, off, size = loc
            _check_header(tar, name, off)
            return h5py.File(_TarSlice(tar, off, size), "r")
        return h5py.File(loc, "r")

    def available(self) -> list[str]:
        return [k for k in super().available() if self._has(self.native(k))]

    def read(self, var, t_idx, cell, members):
        native = self.native(var)
        files = self._files(native)
        ut, inv = np.unique(t_idx, return_inverse=True)
        out = np.full((len(members), len(t_idx)), np.nan, dtype=np.float32)
        for r, m in enumerate(members):
            if int(m) not in files:
                continue
            with self._open(native, int(m)) as h:
                rows = h[native][ut, :]
            out[r] = rows[inv, cell]
        return out


class IconParquet(Source):
    """The ``pyro-icon convert`` store of the hjia monthly PPE (hive-partitioned by
    ``time_label``; member, cell, lat, lon, native variables)."""

    native_kind = "icon"

    def __init__(
        self,
        key,
        ppe_source,
        model,
        resolution,
        root,
        time_kind="interval",
        time_label="end",
        time_step="1mon",
        control_member=None,
        description="",
    ):
        import json

        super().__init__(key, ppe_source, model, resolution, description)
        self.native_kind = "icon"
        self.root = Path(root)
        meta = json.loads((self.root / "metadata.json").read_text())
        stamps = np.array(meta["time_labels"], dtype="datetime64[s]")
        self.time = TimeAxis(stamps, time_kind, time_label, time_step)
        self._labels = meta["time_labels"]
        self._vars = set(meta["variables"])
        g = pl.read_parquet(self.root / "grid.parquet").sort("cell")
        self.grid = UnstructuredGrid(g["lat"].to_numpy(), g["lon"].to_numpy())
        self.members = (
            pl.read_parquet(self.root / "members.parquet")["member"].to_numpy().astype(np.int64)
        )
        self.control_member = control_member

    def available(self) -> list[str]:
        return [k for k in super().available() if self.native(k) in self._vars]

    def read(self, var, t_idx, cell, members):
        native = self.native(var)
        labels = [self._labels[i] for i in np.unique(t_idx)]
        want = pl.DataFrame(
            {
                "t_idx": t_idx.astype(np.int64),
                "cell": cell.astype(np.int64),
                "k": np.arange(len(t_idx)),
            }
        )
        lf = pl.scan_parquet(self.root / "values" / "**" / "*.parquet", hive_partitioning=True)
        got = (
            lf.filter(
                pl.col("time_label").cast(pl.String).is_in(labels)
                & pl.col("cell").is_in(np.unique(cell).tolist())
                & pl.col("member").is_in([int(m) for m in members])
            )
            .select(
                pl.col("time_label").cast(pl.String),
                pl.col("cell").cast(pl.Int64),
                pl.col("member").cast(pl.Int64),
                native,
            )
            .collect()
            .with_columns(
                pl.col("time_label")
                .replace_strict(
                    {lab: i for i, lab in enumerate(self._labels)}, return_dtype=pl.Int64
                )
                .alias("t_idx")
            )
        )
        out = np.full((len(members), len(t_idx)), np.nan, dtype=np.float32)
        for r, m in enumerate(members):
            j = want.join(got.filter(pl.col("member") == int(m)), on=["t_idx", "cell"], how="left")
            out[r] = j.sort("k")[native].to_numpy()
        return out


def _attr(obj, name: str) -> str:
    v = obj.attrs[name]
    return v.decode() if isinstance(v, bytes) else str(v)


READERS = {
    "echam_stacked": EchamStacked,
    "icon_per_variable": IconPerVariable,
    "icon_parquet": IconParquet,
}


# --------------------------------------------------------------------------- config / API


def load_config(path: str | os.PathLike | None = None) -> dict[str, dict]:
    raw = tomllib.loads(Path(path or DEFAULT_CONFIG).read_text())
    return raw.get("sources", {})


def open_source(
    key: str, config: str | os.PathLike | None = None, environ: dict[str, str] | None = None
) -> Source:
    cfg = dict(load_config(config)[key])
    env = os.environ if environ is None else environ
    reader = READERS[cfg.pop("reader")]
    kwargs = {k: expand(v, env) if isinstance(v, str) else v for k, v in cfg.items()}
    return reader(key=key, **kwargs)


def locate(src: Source, points: pl.DataFrame) -> pl.DataFrame:
    """Map points to the source's time index and cell (no data read)."""
    t = points["time"].dt.replace_time_zone(None).cast(pl.Datetime("us")).to_numpy()
    t_idx, dt = src.time.locate(t.astype("datetime64[s]"))
    cell, clat, clon = src.grid.locate(points["lat"].to_numpy(), points["lon"].to_numpy())
    mt = src.time.model_time[np.maximum(t_idx, 0)]
    return points.select("point_id").with_columns(
        pl.Series("t_idx", t_idx, dtype=pl.Int32),
        pl.Series("model_time", mt.astype("datetime64[us]")).dt.replace_time_zone("UTC"),
        pl.Series("dt_hours", dt, dtype=pl.Float64),
        pl.Series("cell", cell, dtype=pl.Int64),
        pl.Series("model_lat", clat, dtype=pl.Float32),
        pl.Series("model_lon", clon, dtype=pl.Float32),
    )


def sample(
    src: Source,
    points: pl.DataFrame,
    variables: list[str],
    members: list[int] | np.ndarray | None = None,
) -> pl.DataFrame:
    """Sample ``variables`` (canonical names, DERIVED allowed) at ``points``.

    Returns the common long schema: one row per (point, member)."""
    mem = np.asarray(src.members if members is None else members, dtype=np.int64)
    need: list[str] = []
    for v in variables:
        for x in DERIVED.get(v, (v,)):
            if x not in need:
                need.append(x)
    loc = locate(src, points)
    valid = (loc["t_idx"] >= 0).to_numpy()
    t_idx = loc["t_idx"].to_numpy().astype(np.int64)
    cell = loc["cell"].to_numpy()
    n = len(points)
    data: dict[str, np.ndarray] = {}
    for v in need:
        arr = np.full((len(mem), n), np.nan, dtype=np.float32)
        if valid.any():
            arr[:, valid] = src.read(v, t_idx[valid], cell[valid], mem)
        data[v] = arr
    for d, (num, den) in DERIVED.items():
        if d in variables:
            with np.errstate(divide="ignore", invalid="ignore"):
                data[d] = (1.0 - data[num] / data[den]).astype(np.float32)
    meta = loc.with_columns(
        pl.lit(src.ppe_source).alias("ppe_source"),
        pl.lit(src.key).alias("dataset"),
        pl.lit(src.model).alias("model"),
        pl.lit(src.resolution).alias("resolution"),
    )
    rep = pl.concat([meta] * len(mem), how="vertical")
    out = rep.with_columns(
        pl.Series("member", np.repeat(mem, n), dtype=pl.Int32),
        pl.Series(
            "is_control",
            np.repeat(mem == (src.control_member if src.control_member is not None else -1), n),
        ),
        *[pl.Series(v, data[v].reshape(-1), dtype=pl.Float32) for v in variables],
    )
    cols = list(COMMON_COLUMNS) + list(variables)
    return out.select(cols).with_columns(
        pl.when(pl.col("t_idx") < 0).then(None).otherwise(pl.col(v)).alias(v) for v in variables
    )


def describe(src: Source) -> dict:
    t = src.time
    return {
        "dataset": src.key,
        "ppe_source": src.ppe_source,
        "model": src.model,
        "resolution": src.resolution,
        "grid": src.grid.name,
        "time_kind": t.kind,
        "first": str(t.starts[0]) + "Z",
        "last": str(t.ends[-1]) + "Z",
        "n_times": int(t.stamps.size),
        "n_members": int(src.members.size),
        "control_member": src.control_member,
        "variables": src.available(),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(prog="pyro-ppe", description=__doc__.splitlines()[0])
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="configured sources")
    d = sub.add_parser("describe", help="open a source and print its axes and variables")
    d.add_argument("source")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "list":
        for k, v in cfg.items():
            print(
                f"{k:28s} {v.get('ppe_source', ''):8s} {v.get('resolution', ''):5s} "
                f"{v.get('description', '')}"
            )
        return 0
    from pyro.envfile import load_env_file

    load_env_file(
        os.environ.get("PYRO_SYNC_ENV_FILE", REPO_ROOT / "config" / "local.env"), os.environ
    )
    print(json.dumps(describe(open_source(a.source, a.config)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
