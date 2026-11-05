import datetime as dt

import polars as pl
import pytest

from pyro import compare

ONSET = dt.datetime(2024, 8, 10, 0, tzinfo=dt.UTC)


def test_events_use_3h_sources_only_by_default(compare_match_dir):
    assert compare.discover_sources(compare_match_dir) == ["echam_3h"]
    assert compare.discover_sources(compare_match_dir, include_coarse=True) == [
        "echam_3h",
        "icon_mon",
    ]
    assert set(compare.load(compare_match_dir).table["dataset"]) == {"echam_3h"}


def test_load_joins_obs_and_ensemble(compare_match_dir):
    cmp = compare.load(compare_match_dir, ["echam_3h", "icon_mon"])
    assert cmp.events == ["EV1"]
    t = cmp.table
    e = t.filter((pl.col("dataset") == "echam_3h") & (pl.col("variable") == "aod550")).sort(
        "obs_time"
    )
    assert e["ppe_source"].unique().to_list() == ["ybhatti"]
    assert e["obs"].to_list() == pytest.approx([0.2, 1.0, 0.4])
    assert e["obs_unc"].to_list() == pytest.approx([0.02] * 3)
    # members 0..10 at point 0: 0.2*(1+0.1m); median at m=5, control m=0
    assert e["model_median"][0] == pytest.approx(0.3)
    assert e["model_control"][0] == pytest.approx(0.2)
    assert e["model_p05"][0] == pytest.approx(0.2 * 1.05)
    assert e["age_days"].to_list() == pytest.approx([-0.5, 1.0, 2.0])
    assert set(e["n_members"]) == {11}

    i = t.filter(pl.col("dataset") == "icon_mon")
    assert i["model_control"].is_null().all()
    # AE missing from that source -> no ae440_670 rows rather than NaNs
    assert set(i["variable"]) == {"aod550", "aaod550", "ssa550"}
    assert i["resolution"].unique().to_list() == ["1mon"]


def test_metrics(compare_match_dir):
    m = compare.metrics(compare.load(compare_match_dir, ["echam_3h", "icon_mon"]).table)
    r = m.filter((pl.col("dataset") == "echam_3h") & (pl.col("variable") == "aod550")).row(
        0, named=True
    )
    med = [0.3, 0.6, 0.9]
    obs = [0.2, 1.0, 0.4]
    assert r["n_superobs"] == 3
    assert r["n_pix"] == 30
    assert r["bias"] == pytest.approx(sum(a - b for a, b in zip(med, obs, strict=True)) / 3)
    assert r["frac_in_5_95"] == pytest.approx(0.0)
    ssa = m.filter((pl.col("dataset") == "echam_3h") & (pl.col("variable") == "ssa550"))
    assert ssa["log10_ratio"].is_null().all()


def test_coverage_lists_every_event(compare_match_dir):
    cov = compare.coverage(compare.load(compare_match_dir, ["echam_3h", "icon_mon"]))
    assert cov["pcb_id"].to_list() == ["EV2", "EV1"]
    ev1 = cov.filter(pl.col("pcb_id") == "EV1").row(0, named=True)
    assert ev1["echam_3h"] == 3 and ev1["icon_mon"] == 1
    assert cov.filter(pl.col("pcb_id") == "EV2")["echam_3h"].is_null().all()


def test_cli_writes_tables_and_figures(compare_match_dir, tmp_path):
    pytest.importorskip("matplotlib")
    out = tmp_path / "out"
    rc = compare.main(
        ["--match-dir", str(compare_match_dir), "-o", str(out), "--env-file", "/nonexistent"]
    )
    assert rc == 0
    for f in ("comparison.parquet", "metrics.parquet", "metrics.csv", "coverage.csv"):
        assert (out / f).exists()
    assert (out / "figures" / "EV1.png").stat().st_size > 10_000
    assert (out / "figures" / "overview.png").exists()


def test_empty_match_dir(tmp_path):
    pl.DataFrame(
        {
            "pcb_id": ["EV1"],
            "region": ["Canada"],
            "kept": [True],
            "onset_utc": [ONSET],
            "lat_c": [60.0],
            "lon_c": [-120.0],
            "n_pix_500km": [0],
        }
    ).write_parquet(tmp_path / "summary.parquet")
    cmp = compare.load(tmp_path)
    assert cmp.events == []
    assert compare.metrics(cmp.table).is_empty()
    assert compare.coverage(cmp)["pcb_id"].to_list() == ["EV1"]
