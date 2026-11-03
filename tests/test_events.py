import pandas as pd
import pytest

from pyro import events

SAMPLE = """pcb_id,onset_utc,region,lon,lat,lon_c,lat_c,kept,is_confirmed
EV_A,2024-07-20 22:00:00,Canada,-120.0,58.0,-120.03,58.01,True,True
EV_B,2024-07-28 22:00:00,Canada,-118.0,56.0,-118.02,56.04,True,True
EV_C,2024-08-10 05:00:00,Northern Asia,120.0,60.0,120.05,59.98,True,True
EV_D,2024-08-12 05:00:00,Northern Asia,121.0,61.0,121.01,61.02,False,True
"""


@pytest.fixture
def table(tmp_path):
    p = tmp_path / "events.csv"
    p.write_text(SAMPLE)
    return p


def test_load_parses_and_validates(table):
    df = events.load_events(table)
    assert str(df["onset_utc"].dt.tz) == "UTC"
    assert df["kept"].dtype == bool
    assert (df["role"] == "pyrocb").all()


def test_missing_column(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("pcb_id,onset_utc,region,lon,lat\nX,2024-01-01,Canada,1,2\n")
    with pytest.raises(events.EventTableError, match="lon_c"):
        events.load_events(p)


def test_duplicate_ids(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text(SAMPLE + "EV_A,2024-07-21 22:00:00,Canada,0,0,0,0,True,True\n")
    with pytest.raises(events.EventTableError, match="duplicate"):
        events.load_events(p)


def test_coverage_counts(table):
    df = events.add_windows(events.load_events(table), pre_days=1, post_days=10)
    cov = events.coverage(df, {"ppe": ("2024-08-01", "2025-08-01")}, by=None)
    row = cov.iloc[0]
    assert row["n_events"] == 4
    assert row["onset_in"] == 2
    assert row["window_full"] == 2
    # EV_B (Jul 28 + 10 d) reaches into August; EV_A (Jul 20 + 10 d) does not.
    assert row["window_any"] == 3


def test_cli_coverage_and_windows(table, tmp_path, capsys):
    assert events.main(["coverage", str(table)]) == 0
    assert "ppe_pd2024_3h" in capsys.readouterr().out
    out = tmp_path / "w.parquet"
    assert events.main(["windows", str(table), "-o", str(out)]) == 0
    w = pd.read_parquet(out)
    assert len(w) == 3
    assert {"window_start", "window_end"} <= set(w.columns)
