from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest
from test_match import SAMPLE, FakeT63

from pyro import events, gridding, viirs
from pyro.regional import load_regions

T0 = datetime(2024, 8, 11, 20, 54, tzinfo=UTC)


def _pix(rows):
    return pl.DataFrame(rows, schema=["time", "lat", "lon", "aod550", "ae", "unc"], orient="row")


def test_grid_regular_bins_like_aerocom_error():
    p = _pix(
        [
            (T0, 58.2, -120.4, 0.2, 1.5, 0.02),
            (T0 + timedelta(minutes=50), 58.7, -120.9, 0.4, 1.7, 0.04),
            (T0, 58.5, 239.5, 0.6, 1.6, 0.06),  # same 1-degree bin after lon wrap
            (T0, 10.0, 10.0, 0.1, 1.0, 0.01),  # alone in its bin -> dropped (min_points=2)
        ]
    )
    g = gridding.grid_regular(
        p, {"aod550": "aod550"}, res_deg=1.0, uncertainty={"aod550": "unc"}, min_points=2
    )
    assert g.height == 1
    r = g.row(0, named=True)
    assert (r["lon_bin"], r["lat_bin"]) == (-120.5, 58.5)
    assert r["n"] == 3 and r["aod550"] == pytest.approx(0.4)
    assert r["aod550_spread"] == pytest.approx(np.std([0.2, 0.4, 0.6], ddof=1))
    assert r["aod550_unc"] == pytest.approx(0.04)  # mean, not reduced by sqrt(N)
    assert r["date"] == datetime(2024, 8, 11, 21, tzinfo=UTC)  # rounded to 3 h
    med = gridding.grid_regular(p, {"aod550": "aod550"}, agg="median", spread="iqr")
    assert med["aod550_spread"][0] == pytest.approx(0.5 * (0.5 - 0.3))


def test_grid_model_and_regional_means():
    p = _pix([(T0, 58.2, -120.4, 0.2, 1.5, 0.02), (T0, 58.3, -120.2, 0.4, 1.5, 0.02)])
    so = gridding.grid_model(p, FakeT63(), {"o_aod550": "aod550"})
    assert so.height == 1 and so["n_pix"][0] == 2
    assert so["o_aod550"][0] == pytest.approx(0.3)
    assert so["resolution"][0] == "3h"

    binned = pl.DataFrame(
        {
            "lat_bin": [50.5, 69.5, 60.5],
            "lon_bin": [-100.5, -100.5, 100.5],
            "x": [1.0, 3.0, 9.0],
            "n": [2, 3, 4],
        }
    )
    rm = gridding.regional_means(binned, load_regions(), ["x"])
    ca = rm.filter(pl.col("region") == "Canada").row(0, named=True)
    w = np.cos(np.radians([50.5, 69.5]))
    assert ca["x"] == pytest.approx((w[0] * 1 + w[1] * 3) / w.sum())
    assert ca["n_bins"] == 2 and ca["n_pix"] == 5
    assert rm.filter(pl.col("region") == "Northern Asia")["x"][0] == pytest.approx(9.0)


def _write_db(path):
    import h5py

    with h5py.File(path, "w") as f:
        f["Latitude"] = np.array([[58.5, 58.6], [62.0, 70.0]], dtype=np.float32)
        f["Longitude"] = np.array([[-120.0, -120.1], [-120.0, -120.0]], dtype=np.float32)
        secs = (datetime(2024, 8, 11, 20, 55) - datetime(1993, 1, 1)).total_seconds()
        t = f.create_dataset("Scan_Start_Time", data=np.full((2, 2), secs))
        t.attrs["units"] = "seconds since 1993-01-01 00:00:00"
        a = f.create_dataset(
            "Aerosol_Optical_Thickness_550_Land_Ocean_Best_Estimate",
            data=np.array([[0.8, 1.0], [0.1, -999.0]], dtype=np.float32),
        )
        a.attrs["_FillValue"] = np.float32(-999.0)
        f["Angstrom_Exponent_Land_Ocean_Best_Estimate"] = np.full((2, 2), 1.4, np.float32)


def _write_dt(path):
    import h5py

    with h5py.File(path, "w") as f:
        g = f.create_group("geolocation_data")
        g["latitude"] = np.array([[58.5]], dtype=np.float32)
        g["longitude"] = np.array([[-120.0]], dtype=np.float32)
        d = f.create_group("geophysical_data").create_dataset(
            "Optical_Depth_Land_And_Ocean", data=np.array([[500]], dtype=np.int16)
        )
        d.attrs["scale_factor"] = 0.001
        d.attrs["_FillValue"] = np.int16(-9999)


def test_read_granules_and_extract(tmp_path):
    db = tmp_path / "AERDB_L2_VIIRS_SNPP.A2024224.2054.002.2024225001122.nc"
    dtf = tmp_path / "AERDT_L2_VIIRS_NOAA20.A2024224.2100.002.2024225001122.nc"
    _write_db(db)
    _write_dt(dtf)
    d = viirs.read_granule(db)
    assert d.height == 3  # fill value dropped
    assert d["time"][0] == datetime(2024, 8, 11, 20, 55, tzinfo=UTC)
    assert set(d["product"]) == {"AERDB_L2_VIIRS_SNPP"} and set(d["platform"]) == {"SNPP"}
    t = viirs.read_granule(dtf)
    assert t["aod550"].to_list() == pytest.approx([0.5])
    assert t["time"][0] == datetime(2024, 8, 11, 21, 0, tzinfo=UTC)  # file-name fallback
    assert any("Optical_Depth_Land_And_Ocean" in s for s in viirs.inspect(dtf))

    written = viirs.extract([db, dtf], tmp_path / "pix")
    assert sorted(p.name for p in written) == [
        "AERDB_L2_VIIRS_SNPP_2024_08.parquet",
        "AERDT_L2_VIIRS_NOAA20_2024_08.parquet",
    ]


def test_match_event_viirs(tmp_path):
    db = tmp_path / "AERDB_L2_VIIRS_SNPP.A2024224.2054.002.2024225001122.nc"
    _write_db(db)
    viirs.extract([db], tmp_path / "pix")
    p = tmp_path / "e.csv"
    p.write_text(SAMPLE)
    ev = events.load_events(p)
    pix = viirs.load_pixels(tmp_path / "pix", events.add_windows(ev, 1, 10))
    so, model = viirs.match_event_viirs(ev, pix, FakeT63(), radius_km=500)
    ca = so.filter(pl.col("pcb_id") == "EV_CA")
    # 58.5/58.6 N share a T63 cell (0.8, 1.0); 62 N is 445 km away in the next row
    assert ca["n_pix"].sum() == 3 and set(ca["product"]) == {"AERDB_L2_VIIRS_SNPP"}
    assert model.height == so.height * 3 and "od550aer" in model.columns
    src = FakeT63()
    src.resolution = "6h"
    with pytest.raises(ValueError, match="3-hourly"):
        viirs.match_event_viirs(ev, pix, src)


def test_viirs_superobs_feed_compare(tmp_path):
    from pyro import compare

    db = tmp_path / "AERDB_L2_VIIRS_SNPP.A2024224.2054.002.2024225001122.nc"
    _write_db(db)
    viirs.extract([db], tmp_path / "pix")
    p = tmp_path / "e.csv"
    p.write_text(SAMPLE)
    ev = events.load_events(p)
    pix = viirs.load_pixels(tmp_path / "pix", events.add_windows(ev, 1, 10))
    so, model = viirs.match_event_viirs(ev, pix, FakeT63(), radius_km=500)
    d = tmp_path / "match"
    d.mkdir()
    so.write_parquet(d / "superobs_fake_3h.parquet")
    model.write_parquet(d / "model_fake_3h.parquet")
    from pyro.match import summarize

    summarize(events.add_windows(ev, 1, 10), pl.DataFrame()).write_parquet(d / "summary.parquet")
    cmp = compare.load(d)
    assert set(cmp.table["variable"]) == {"aod550"}
    assert set(cmp.table["product"]) == {"AERDB_L2_VIIRS_SNPP"}
