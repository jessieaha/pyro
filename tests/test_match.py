from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from pyro import events, match, ppe

SAMPLE = """pcb_id,onset_utc,region,lon,lat,lon_c,lat_c,kept
EV_CA,2024-08-10 22:00:00,Canada,-120.0,58.0,-120.0,58.0,True
EV_AS,2024-08-12 05:00:00,Northern Asia,179.5,65.0,179.5,65.0,True
EV_AK,2024-06-20 23:00:00,Alaska,-150.0,64.0,-150.0,64.0,False
"""


def _spex_rows(lat, lon, t, aot, n=1):
    return [
        {
            "lat": lat,
            "lon": lon,
            "date": t,
            "aot_440nm": aot * 1.6,
            "aot_550nm": aot,
            "aot_670nm": aot * 0.8,
            "aot_865nm": aot * 0.6,
            "aot_uncertainty_550nm": 0.02,
            "ssa_440nm": 0.93,
            "ssa_550nm": 0.94,
            "ssa_uncertainty_550nm": 0.02,
            "surface": "land",
        }
    ] * n


@pytest.fixture
def evs(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text(SAMPLE)
    return events.add_windows(events.load_events(p), 1, 10)


@pytest.fixture
def spex():
    t = datetime(2024, 8, 11, 20, tzinfo=UTC)
    rows = (
        _spex_rows(58.5, -120.0, t, 0.8, 3)  # ~56 km from EV_CA, smoky, early
        + _spex_rows(62.0, -120.0, t + timedelta(days=5), 0.1)  # ~445 km, clean
        + _spex_rows(58.0, -120.0, t + timedelta(days=30), 2.0)  # outside window
        + _spex_rows(65.0, -179.5, t, 0.6)  # across the dateline from EV_AS (~47 km)
    )
    return pl.DataFrame(rows).with_columns(
        pl.col("lat", "lon", "^aot.*$", "^ssa.*$").cast(pl.Float32)
    )


def test_match_window_radius_and_dateline(evs, spex):
    m = match.match_events(evs, spex, 1000)
    assert set(m["pcb_id"]) == {"EV_CA", "EV_AS"}
    ca = m.filter(pl.col("pcb_id") == "EV_CA")
    assert len(ca) == 4
    assert ca["dist_km"].max() < 500
    as_ = m.filter(pl.col("pcb_id") == "EV_AS")
    assert as_["dist_km"][0] == pytest.approx(47.0, abs=2)


def test_summary_keeps_all_events_and_regions(evs, spex):
    s = match.summarize(evs, match.match_events(evs, spex, 1000))
    assert s["pcb_id"].to_list() == ["EV_AK", "EV_CA", "EV_AS"]
    row = s.filter(pl.col("pcb_id") == "EV_CA").row(0, named=True)
    assert row["n_pix_150km"] == 3 and row["n_pix_500km"] == 4
    assert row["n_pix_smoky_150km_m1d_p2d"] == 3
    assert row["smoky_near_early"] and row["onset_in_ppe_3h"]
    ak = s.filter(pl.col("pcb_id") == "EV_AK").row(0, named=True)
    assert ak["n_pix_500km"] == 0 and not ak["matched_500km"] and not ak["onset_in_ppe_3h"]


class FakeT63(ppe.Source):
    """3-hourly T63 source whose AOD encodes the member, AAOD fixed at 0.01."""

    native_kind = "echam"

    def __init__(self):
        super().__init__("fake_3h", "ybhatti", "ECHAM", "3h")
        self.grid = ppe.GaussianGrid(np.linspace(88.6, -88.6, 96), np.arange(192) * 1.875)
        start = np.datetime64("2024-08-01T00:00:00")
        self.time = ppe.TimeAxis(start + np.arange(2920) * np.timedelta64(3, "h"))
        self.members = np.arange(3)
        self.control_member = 0

    def available(self):
        return ["od550aer", "abs550aer"]

    def read(self, var, t_idx, cell, members):
        base = np.asarray(members, dtype=np.float32)[:, None] + 1
        return np.broadcast_to(
            0.1 * base if var == "od550aer" else 0.01 + 0 * base, (len(members), len(t_idx))
        ).copy()


def test_superobs_maps_to_model_grid_and_step(evs, spex):
    src = FakeT63()
    so = match.superobs(match.match_events(evs, spex, 1000), src, 500)
    ca = so.filter(pl.col("pcb_id") == "EV_CA").sort("t_idx")
    first = ca.row(0, named=True)
    # 2024-08-11 20 UTC -> 10 days + 20 h after the 2024-08-01 start of the 3-hourly axis
    assert first["t_idx"] == round((10 + 20 / 24) / 0.125)
    assert first["n_pix"] == 3
    j, i = src.grid.split(np.array([first["cell"]]))
    assert i[0] == round((-120.0 % 360) / 1.875)
    assert first["dataset"] == "fake_3h" and first["ppe_source"] == "ybhatti"
    assert first["o_aaod550"] == pytest.approx(0.8 * (1 - 0.94), rel=1e-5)
    assert first["o_ae440_670"] == pytest.approx(-np.log(1.6 / 0.8) / np.log(440 / 670), rel=1e-5)

    model = match.sample_model(so, src)
    assert model.height == so.height * 3
    assert {"od550aer", "abs550aer", "ssa550aer"} <= set(model.columns)
    assert "ae440_670" not in model.columns  # not provided by this source
    m2 = model.filter((pl.col("pcb_id") == "EV_CA") & (pl.col("member") == 2))
    assert m2["od550aer"][0] == pytest.approx(0.3)
    assert m2["ssa550aer"][0] == pytest.approx(1 - 0.01 / 0.3, rel=1e-5)
    assert model.filter(pl.col("member") == 0)["is_control"].all()
