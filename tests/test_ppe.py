import tarfile
from datetime import UTC, datetime

import numpy as np
import polars as pl
import pytest

from pyro import icon_ppe, ppe

netCDF4 = pytest.importorskip("netCDF4")
pytest.importorskip("h5py")

# ---------------------------------------------------------------- synthetic ECHAM

E_LAT = np.array([60.0, 20.0, -20.0, -60.0])
E_LON = np.arange(0.0, 360.0, 90.0)
E_MEMBERS = [0, 1, 2]
E_HOURS = np.arange(8) * 3.0  # 2024-08-01 00Z .. 21Z
E_T0 = (np.datetime64("2024-08-01") - np.datetime64("2024-04-01")).astype(int)


def _echam_value(var, m, t, j, i):
    return (1 + m) * 0.1 + t * 0.01 + j * 0.001 + i * 0.0001 + (0.5 if var.startswith("ABS") else 0)


def _echam_field(var, m):
    t, j, i = np.meshgrid(range(8), range(4), range(4), indexing="ij")
    return _echam_value(var, m, t, j, i).astype(np.float32)


def _echam_axes(ds):
    ds.createDimension("time", None)
    ds.createDimension("lat", 4)
    ds.createDimension("lon", 4)
    t = ds.createVariable("time", "f8", ("time",))
    t.units = "days since 2024-4-1 00:00:00"
    t[:] = E_T0 + E_HOURS / 24
    ds.createVariable("lat", "f8", ("lat",))[:] = E_LAT
    ds.createVariable("lon", "f8", ("lon",))[:] = E_LON


@pytest.fixture
def echam_root(tmp_path):
    stacked = tmp_path / "stacked"
    stacked.mkdir()
    for var in ("TAU_2D_550nm", "ABS_2D_550nm"):
        with netCDF4.Dataset(stacked / f"{var}_hifreq_PPE.nc", "w") as ds:
            _echam_axes(ds)
            ds.createDimension("ensemble", len(E_MEMBERS))
            ds.createVariable("ensemble", "i8", ("ensemble",))[:] = E_MEMBERS
            v = ds.createVariable(var, "f4", ("ensemble", "time", "lat", "lon"))
            v[:] = np.stack([_echam_field(var, m) for m in E_MEMBERS])
    members = tmp_path / "members"
    for m in E_MEMBERS:
        name = "PPE_ENS_Control" if m == 0 else f"PPE_ENS_{m}"
        (members / name).mkdir(parents=True)
        # member 1 only has the full hifreq file, the others a per-variable extract
        fname = f"{name}_202408.01_hifreq.nc" if m == 1 else "ANG_550nm_865nm_hifreq.nc"
        with netCDF4.Dataset(members / name / fname, "w") as ds:
            _echam_axes(ds)
            v = ds.createVariable("ANG_550nm_865nm", "f4", ("time", "lat", "lon"))
            v[:] = _echam_field("ANG", m)
    return stacked, members


# ---------------------------------------------------------------- synthetic ICON

I_LAT = np.array([60.0, 20.0, -20.0, -60.0, 58.0])
I_LON = np.array([-120.0, 90.0, 180.0, 0.0, 170.0])
I_MEMBERS = [0, 1, 3]


def _icon_value(var, m, t, c):
    return 0.2 * (1 + m) + 0.02 * t + 0.002 * c + (0.3 if var.startswith("ABS") else 0)


def _write_icon(path, var, m, times):
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("time", None)
        ds.createDimension("ncells", I_LAT.size)
        ds.createDimension("vertices", 3)
        t = ds.createVariable("time", "f8", ("time",))
        t.units = "day as %Y%m%d.%f"
        t[:] = times
        for name, val in (("clat", I_LAT), ("clon", I_LON)):
            ds.createVariable(name, "f8", ("ncells",))[:] = np.radians(val)
            b = ds.createVariable(f"{name}_bnds", "f8", ("ncells", "vertices"))
            b[:] = np.radians(np.repeat(val[:, None], 3, axis=1))
        v = ds.createVariable(var, "f4", ("time", "ncells"))
        v.units = "1"
        tt, cc = np.meshgrid(range(len(times)), range(I_LAT.size), indexing="ij")
        v[:] = _icon_value(var, m, tt, cc)


SIX_HOURLY = [20241001.25, 20241001.5, 20241001.75, 20241002.0]
MONTHLY = [20240601.0, 20240701.0, 20240801.0]


def _icon_tree(root, times):
    for var in ("TAU_2D_550nm", "ABS_2D_550nm"):
        (root / var).mkdir(parents=True)
        for m in I_MEMBERS:
            _write_icon(root / var / f"ens_{m:03d}_PD_{var}.nc", var, m, times)
    return root


def _pack_dmftar(src, dst, write_idx=True):
    """Pack <src>/<VAR>/ into <dst>/<VAR>.dmftar/0000/<VAR>.dmftar.tar (+ tar -tv .idx)."""
    for d in sorted(p for p in src.iterdir() if p.is_dir()):
        out = dst / f"{d.name}.dmftar" / "0000"
        out.mkdir(parents=True)
        tar = out / f"{d.name}.dmftar.tar"
        with tarfile.open(tar, "w", format=tarfile.GNU_FORMAT) as tf:
            tf.add(d, arcname=d.name)
        if write_idx:
            lines = []
            with tarfile.open(tar) as tf:
                for m in tf:
                    kind = "d" if m.isdir() else "-"
                    name = m.name + ("/" if m.isdir() else "")
                    lines.append(f"{kind}rw-rw---- u/g {m.size} 2026-01-01 00:00 {name}")
            (tar.parent / (tar.name + ".idx")).write_text("\n".join(lines) + "\n")
    return dst


@pytest.fixture
def icon_6h(tmp_path):
    return _icon_tree(tmp_path / "icon6h", SIX_HOURLY)


@pytest.fixture
def icon_monthly(tmp_path):
    return _icon_tree(tmp_path / "icon_mon", MONTHLY)


def _points(rows):
    return pl.DataFrame(
        {
            "point_id": list(range(len(rows))),
            "time": [r[0] for r in rows],
            "lat": [r[1] for r in rows],
            "lon": [r[2] for r in rows],
        },
        schema_overrides={"time": pl.Datetime("us", "UTC")},
    )


def _common(src, **kw):
    return {"key": "k", "ppe_source": src, "model": "m", "resolution": "r", **kw}


# ---------------------------------------------------------------- tests


def test_time_decoding():
    t = ppe._to_datetime64(np.array([20241001.25, 20241002.0]), "day as %Y%m%d.%f")
    assert t.tolist() == [datetime(2024, 10, 1, 6), datetime(2024, 10, 2)]
    t = ppe._to_datetime64(np.array([122.125]), "days since 2024-4-1 00:00:00")
    assert t[0] == np.datetime64("2024-08-01T03:00")


def test_monthly_interval_end_label():
    ax = ppe.TimeAxis(
        np.array(["2024-06-01", "2024-07-01"], dtype="datetime64[s]"), "interval", "end", "1mon"
    )
    idx, dt = ax.locate(
        np.array(
            ["2024-05-15T12", "2024-06-30T23", "2024-07-01", "2024-04-30"], dtype="datetime64[s]"
        )
    )
    assert idx.tolist() == [0, 1, -1, -1]
    assert ax.model_time[0] == np.datetime64("2024-05-16T12:00")
    assert np.isnan(dt[2:]).all()
    start = ppe.TimeAxis(ax.stamps, "interval", "start", "1mon")
    assert start.locate(np.array(["2024-06-15"], dtype="datetime64[s]"))[0].tolist() == [0]


def test_instant_nearest_and_out_of_period():
    s = np.arange("2024-08-01T00", "2024-08-02T00", 3, dtype="datetime64[h]").astype(
        "datetime64[s]"
    )
    ax = ppe.TimeAxis(s)
    idx, dt = ax.locate(
        np.array(
            ["2024-08-01T04:20", "2024-08-01T22:00", "2024-08-02T03:00", "2024-07-31T22:00"],
            dtype="datetime64[s]",
        )
    )
    assert idx.tolist() == [1, 7, -1, -1]
    assert dt[0] == pytest.approx(4 / 3)


def test_gaussian_grid_wraps_longitude():
    g = ppe.GaussianGrid(E_LAT, E_LON)
    cell, lat, lon = g.locate(np.array([55.0, -50.0]), np.array([-95.0, 350.0]))
    assert g.split(cell)[0].tolist() == [0, 3]
    assert g.split(cell)[1].tolist() == [3, 0]
    assert lon.tolist() == [-90.0, 0.0]


def test_echam_stacked_and_member_fallback(echam_root):
    stacked, members = echam_root
    src = ppe.EchamStacked(**_common("ybhatti"), root=stacked, member_root=members)
    assert src.control_member == 0 and src.members.tolist() == E_MEMBERS
    pts = _points([(datetime(2024, 8, 1, 7, 0, tzinfo=UTC), 21.0, 185.0)])
    df = ppe.sample(src, pts, ["od550aer", "ssa550aer", "ae550_865"])
    assert df.columns == list(ppe.COMMON_COLUMNS) + ["od550aer", "ssa550aer", "ae550_865"]
    assert df["is_control"].to_list() == [True, False, False]
    # 07Z -> nearest 06Z step (t=2); lat 21 -> j=1; lon 185 -> i=2 (180E -> -180)
    assert df["t_idx"].to_list() == [2] * 3 and df["model_lon"][0] == -180.0
    tau = np.array([_echam_value("TAU", m, 2, 1, 2) for m in E_MEMBERS])
    ab = np.array([_echam_value("ABS", m, 2, 1, 2) for m in E_MEMBERS])
    assert df["od550aer"].to_numpy() == pytest.approx(tau, rel=1e-6)
    assert df["ssa550aer"].to_numpy() == pytest.approx(1 - ab / tau, rel=1e-5)
    ang = [_echam_value("ANG", m, 2, 1, 2) for m in E_MEMBERS]
    assert df["ae550_865"].to_numpy() == pytest.approx(ang, rel=1e-6)
    assert df["dt_hours"][0] == pytest.approx(1.0)
    with pytest.raises(KeyError):
        ppe.sample(src, pts, ["emibc"])


def test_icon_plain_and_archive_identical(icon_6h, tmp_path):
    plain = ppe.IconPerVariable(**_common("hjia"), root=icon_6h)
    arch_root = _pack_dmftar(icon_6h, tmp_path / "archive")
    arch = ppe.IconPerVariable(**_common("hjia"), root=arch_root, archive=True)
    noidx = ppe.IconPerVariable(
        **_common("hjia"), root=_pack_dmftar(icon_6h, tmp_path / "noidx", False), archive=True
    )
    pts = _points(
        [
            (datetime(2024, 10, 1, 13, 0, tzinfo=UTC), 59.0, 240.0),  # 12Z step, cell 0
            (datetime(2024, 10, 2, 1, 0, tzinfo=UTC), 57.0, -190.0),  # 00Z step, cell 4
            (datetime(2024, 9, 30, 12, 0, tzinfo=UTC), 0.0, 0.0),  # before the first step
        ]
    )
    a = ppe.sample(plain, pts, ["od550aer", "ssa550aer"])
    assert a.equals(ppe.sample(arch, pts, ["od550aer", "ssa550aer"]))
    assert a.equals(ppe.sample(noidx, pts, ["od550aer", "ssa550aer"]))
    m1 = a.filter(pl.col("member") == 1)
    assert m1["t_idx"].to_list() == [1, 3, -1]
    assert m1["cell"].to_list()[:2] == [0, 4]
    assert m1["od550aer"][0] == pytest.approx(_icon_value("TAU", 1, 1, 0), rel=1e-6)
    assert m1["od550aer"][2] is None and m1["ssa550aer"][2] is None
    assert not a["is_control"].any()


def test_archive_index_mismatch_detected(icon_6h, tmp_path):
    root = _pack_dmftar(icon_6h, tmp_path / "archive")
    tar = root / "TAU_2D_550nm.dmftar" / "0000" / "TAU_2D_550nm.dmftar.tar"
    idx = tar.parent / (tar.name + ".idx")
    lines = idx.read_text().splitlines()
    lines[1], lines[2] = lines[2], lines[1]  # entries listed out of tar order
    idx.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="index points to"):
        ppe.IconPerVariable(**_common("hjia"), root=root, archive=True)


def test_monthly_parquet_matches_netcdf(icon_monthly, tmp_path):
    store = tmp_path / "store"
    icon_ppe.convert(icon_monthly, store, tmp_path / "work", workers=1)
    kw = {"time_kind": "interval", "time_label": "end", "time_step": "1mon"}
    nc = ppe.IconPerVariable(**_common("hjia"), root=icon_monthly, **kw)
    pq = ppe.IconParquet(**_common("hjia"), root=store, **kw)
    pts = _points(
        [
            (datetime(2024, 6, 20, tzinfo=UTC), 21.0, 91.0),  # label 2024-07-01 = June mean
            (datetime(2024, 5, 2, tzinfo=UTC), -60.0, 0.0),  # label 2024-06-01 = May mean
            (datetime(2024, 8, 2, tzinfo=UTC), -60.0, 0.0),  # after the last mean
        ]
    )
    a = ppe.sample(nc, pts, ["od550aer", "ssa550aer"])
    b = ppe.sample(pq, pts, ["od550aer", "ssa550aer"])
    assert a.schema == b.schema
    assert a["t_idx"].to_list()[:3] == [1, 0, -1]
    vals = ["t_idx", "cell", "member", "od550aer", "ssa550aer"]
    assert a.select(vals).equals(b.select(vals))
    assert a["model_time"][0] == datetime(2024, 6, 16, tzinfo=UTC)


def test_sources_share_one_schema(echam_root, icon_6h):
    stacked, members = echam_root
    e = ppe.EchamStacked(**_common("ybhatti"), root=stacked, member_root=members)
    i = ppe.IconPerVariable(**_common("hjia"), root=icon_6h)
    pts = _points([(datetime(2024, 8, 1, 3, tzinfo=UTC), 60.0, 0.0)])
    a = ppe.sample(e, pts, ["od550aer", "ssa550aer"])
    b = ppe.sample(i, pts, ["od550aer", "ssa550aer"])
    assert a.schema == b.schema
    assert dict(list(a.schema.items())[: len(ppe.COMMON_COLUMNS)]) == ppe.COMMON_COLUMNS
    assert set(pl.concat([a, b])["ppe_source"]) == {"ybhatti", "hjia"}


def test_open_source_from_config(tmp_path, icon_6h):
    cfg = tmp_path / "ppe.toml"
    cfg.write_text(
        '[sources.x]\nreader = "icon_per_variable"\nppe_source = "hjia"\nmodel = "ICON-HAM"\n'
        'resolution = "6h"\nroot = "${HJIA_ROOT}"\n'
    )
    src = ppe.open_source("x", cfg, {"HJIA_ROOT": str(icon_6h)})
    assert ppe.describe(src)["n_members"] == 3
    with pytest.raises(ValueError):
        ppe.IconPerVariable(**_common("other"), root=icon_6h)


def test_repo_config_has_no_literal_paths():
    for key, cfg in ppe.load_config().items():
        assert cfg["ppe_source"] in ppe.PPE_SOURCES, key
        assert cfg["root"].startswith("${"), key
