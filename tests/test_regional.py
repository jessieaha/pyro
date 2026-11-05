from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest
from test_match import SAMPLE, FakeT63, _spex_rows

from pyro import events, ppe, regional

REGIONS = """
[regions.Alaska]
lat = [55.0, 72.0]
lon = [-170.0, -141.0]

[regions.Canada]
lat = [48.0, 70.0]
lon = [-141.0, -60.0]

[regions."Northern Asia"]
lat = [45.0, 72.0]
lon = [60.0, 180.0]
"""


@pytest.fixture
def regions(tmp_path):
    p = tmp_path / "regions.toml"
    p.write_text(REGIONS)
    return regional.load_regions(p)


@pytest.fixture
def evs(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text(SAMPLE)
    return events.add_windows(events.load_events(p), 1, 10)


def _pixels():
    t = datetime(2024, 8, 11, 20, tzinfo=UTC)
    rows = (
        _spex_rows(58.5, -120.0, t, 0.8, 3)  # near EV_CA, in window -> pyrocb
        + _spex_rows(58.5, 240.0, t + timedelta(days=30), 0.2)  # same place, later -> background
        + _spex_rows(50.0, -70.0, t, 0.1)  # eastern Canada, far -> background
        + _spex_rows(65.0, -150.0, t, 0.3)  # Alaska box; EV_AK window is June -> background
        + _spex_rows(30.0, -120.0, t, 0.3)  # outside every box -> dropped
    )
    return pl.DataFrame(rows).with_columns(
        pl.col("lat", "lon", "^aot.*$", "^ssa.*$").cast(pl.Float32)
    )


def test_overlapping_regions_rejected(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text("[regions.A]\nlat=[0,10]\nlon=[0,10]\n[regions.B]\nlat=[5,15]\nlon=[5,15]\n")
    with pytest.raises(ValueError, match="overlap"):
        regional.load_regions(p)


def test_repo_regions_load():
    r = regional.load_regions()
    assert set(r["region"]) == {"Alaska", "Canada", "Northern Asia"}


def test_assign_and_classify(regions, evs):
    pix = regional.assign_region(_pixels(), regions)
    assert pix.height == 6
    assert pix.filter(pl.col("lon") == 240.0)["region"].to_list() == ["Canada"]
    c = regional.classify(pix, evs, 500)
    py = c.filter(pl.col("category") == "pyrocb")
    assert py.height == 3
    assert set(py["pcb_id"]) == {"EV_CA"} and (py["dist_km"] < 100).all()
    bg = c.filter(pl.col("category") == "background")
    assert bg.height == 3 and bg["pcb_id"].is_null().all()
    assert set(bg["region"]) == {"Canada", "Alaska"}


def test_aeroval_statistics():
    t = datetime(2024, 8, 2, tzinfo=UTC)
    obs = np.array([0.1, 0.2, 0.4, 0.8])
    mod = np.array([0.2, 0.2, 0.3, 1.0])
    df = pl.DataFrame(
        {
            "region": ["Canada"] * 4,
            "category": ["background", "background", "pyrocb", "pyrocb"],
            "obs_time": [t] * 4,
            "dataset": ["d"] * 4,
            "variable": ["aod550"] * 4,
            "obs": obs,
            "mod": mod,
        }
    )
    st = regional.stats(df)
    assert set(st["period"]) == {"season", "2024-08"}
    assert set(st["category"]) == {"all", "background", "pyrocb"}
    a = st.filter((pl.col("category") == "all") & (pl.col("period") == "season")).row(0, named=True)
    d = mod - obs
    assert a["n"] == 4
    assert a["bias"] == pytest.approx(d.mean())
    assert a["nmb"] == pytest.approx(d.sum() / obs.sum())
    assert a["mnmb"] == pytest.approx(np.mean(2 * d / (mod + obs)))
    assert a["fge"] == pytest.approx(np.mean(2 * np.abs(d) / (mod + obs)))
    assert a["rmse"] == pytest.approx(np.sqrt(np.mean(d**2)))
    assert a["r"] == pytest.approx(np.corrcoef(obs, mod)[0, 1])
    p = st.filter((pl.col("category") == "pyrocb") & (pl.col("period") == "season"))
    assert p["n"][0] == 2 and p["bias"][0] == pytest.approx(0.05)


def test_run_end_to_end(tmp_path, regions, evs, monkeypatch):
    pytest.importorskip("matplotlib")
    spex = tmp_path / "spex"
    spex.mkdir()
    _pixels().drop("surface").write_parquet(
        spex / "PACE_SPEXONE_L2_AER_RTAPLAND_2024_08_v4.0.parquet"
    )
    _pixels().drop("surface").write_parquet(
        spex / "PACE_SPEXONE_L2_AER_RTAPLAND_2024_09_v4.0.parquet"
    )
    (tmp_path / "regions.toml").write_text(REGIONS)
    (tmp_path / "e.csv").write_text(SAMPLE)
    monkeypatch.setattr(ppe, "open_source", lambda key, cfg=None: FakeT63())
    out = tmp_path / "out"
    res = regional.run(
        tmp_path / "e.csv",
        spex,
        out,
        ["fake_3h"],
        regions_path=tmp_path / "regions.toml",
        end=datetime(2024, 9, 30, tzinfo=UTC),
    )
    st = res["stats"]
    assert set(st["estimate"]) == {"ensemble_median", "control"}
    med = st.filter(
        (pl.col("estimate") == "ensemble_median")
        & (pl.col("period") == "season")
        & (pl.col("variable") == "aod550")
    )
    # FakeT63 AOD = 0.1 * (member + 1); median over members 0..2 = 0.2
    py = med.filter((pl.col("region") == "Canada") & (pl.col("category") == "pyrocb")).row(
        0, named=True
    )
    assert py["mean_mod"] == pytest.approx(0.2) and py["mean_obs"] == pytest.approx(0.8)
    assert py["n"] == 1  # identical pixels from both files fall into one super-obs
    so = pl.read_parquet(out / "superobs_fake_3h.parquet")
    assert so.filter(pl.col("category") == "pyrocb")["n_pix"].to_list() == [6]
    summ = res["member_summary"]
    assert (summ["n_members"] == 3).all()
    ctl = st.filter((pl.col("estimate") == "control") & (pl.col("variable") == "aod550"))
    assert ctl["mean_mod"].to_list() == pytest.approx([0.1] * ctl.height)
    for f in (
        "stats.parquet",
        "stats.csv",
        "member_stats.parquet",
        "member_summary.csv",
        "superobs_fake_3h.parquet",
        "model_fake_3h.parquet",
        "figures/regions_season.png",
    ):
        assert (out / f).exists(), f
