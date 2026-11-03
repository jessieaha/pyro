import json
import shutil

import numpy as np
import polars as pl
import pytest

from pyro import icon_ppe, store


def _write_member(path, var, data, lat, lon):
    netCDF4 = pytest.importorskip("netCDF4")
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("time", None)
        ds.createDimension("ncells", lat.size)
        ds.createDimension("vertices", 3)
        t = ds.createVariable("time", "f8", ("time",))
        t.units = "day as %Y%m%d.%f"
        t[:] = [20240601.0, 20240701.0]
        for name, val in (("clat", lat), ("clon", lon)):
            v = ds.createVariable(name, "f4", ("ncells",))
            v[:] = val
            b = ds.createVariable(f"{name}_bnds", "f4", ("ncells", "vertices"))
            b[:] = np.repeat(val[:, None], 3, axis=1)
        v = ds.createVariable(var, "f8", ("time", "ncells"))
        v.units = "1"
        v.long_name = f"test {var}"
        v[:] = data


@pytest.fixture
def icon_src(tmp_path):
    rng = np.random.default_rng(1)
    lat = np.radians(np.array([10.0, -30.0, 60.0, 45.0], dtype=np.float32))
    lon = np.radians(np.array([0.0, 90.0, -120.0, 150.0], dtype=np.float32))
    src = tmp_path / "PD"
    for var in ("TAU_2D_550nm", "emi_bc"):
        (src / var).mkdir(parents=True)
        for mem in (0, 1, 3):
            if var == "emi_bc" and mem == 3:
                continue
            data = rng.random((2, 4)) * (1e-12 if var == "emi_bc" else 1)
            _write_member(src / var / f"ens_{mem:03d}_PD_{var}.nc", var, data, lat, lon)
        (src / var / "summary.csv").write_text("ens\n")
    return src


def test_icon_convert_roundtrip(icon_src, tmp_path):
    out = tmp_path / "store"
    meta = icon_ppe.convert(icon_src, out, tmp_path / "work", workers=1)
    assert meta["time_labels"] == ["2024-06-01", "2024-07-01"]
    assert meta["members_missing"] == [2]
    assert icon_ppe.verify(icon_src, out, n_samples=6) == 0
    lf = store.scan(str(out / "values"))
    df = lf.collect()
    assert df.height == 2 * 4 * 3
    for part in df.partition_by("time_label"):
        assert part["lat"].to_list() == sorted(part["lat"].to_list())
    missing = df.filter(pl.col("member") == 3)["emi_bc"]
    assert missing.is_nan().all()
    north = lf.filter(pl.col("lat") > 40).select("cell").unique().collect()
    assert set(north["cell"]) == {2, 3}
    m = json.loads((out / "metadata.json").read_text())
    assert m["variables"]["TAU_2D_550nm"]["max_rel_err_float32"] < 1e-7


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")
def test_query_through_served_remote(icon_src, tmp_path):
    pytest.importorskip("s3fs")
    out = tmp_path / "remote" / "pyro" / "model"
    icon_ppe.convert(icon_src, out, tmp_path / "work", workers=1)
    with store.served(str(tmp_path / "remote" / "pyro")) as srv:
        lf = store.scan(srv.path("model/values"), srv.filesystem)
        got = (
            lf.filter(pl.col("lat") > 40)
            .group_by("time_label")
            .agg(pl.col("TAU_2D_550nm").mean())
            .collect()
        )
    local = (
        store.scan(str(out / "values"))
        .filter(pl.col("lat") > 40)
        .group_by("time_label")
        .agg(pl.col("TAU_2D_550nm").mean())
        .collect()
    )
    assert got.sort("time_label")["TAU_2D_550nm"].to_list() == pytest.approx(
        local.sort("time_label")["TAU_2D_550nm"].to_list()
    )
    assert srv.process.poll() is not None
