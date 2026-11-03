import polars as pl
import pytest

from pyro import events

SAMPLE = """pcb_id,onset_utc,region,lon,lat,lon_c,lat_c,kept,is_confirmed
EV_A,2024-07-20 22:00:00,Canada,-120.0,58.0,-120.03,58.01,True,True
EV_B,2024-07-28 22:00:00,Canada,-118.0,56.0,-118.02,56.04,True,True
EV_C,2024-08-10 05:00:00,Northern Asia,120.0,60.0,120.05,59.98,True,True
EV_D,2024-08-12 05:00:00,Northern Asia,121.0,61.0,121.01,61.02,False,True
EV_E,2024-08-20 23:00:00,Alaska,-150.0,64.0,-150.04,64.02,False,False
"""


@pytest.fixture
def table(tmp_path):
    p = tmp_path / "events.csv"
    p.write_text(SAMPLE)
    return p


def test_load_parses_and_validates(table):
    df = events.load_events(table)
    assert isinstance(df, pl.DataFrame)
    assert df.schema["onset_utc"] == pl.Datetime("us", "UTC")
    assert df.schema["kept"] == pl.Boolean
    assert df["role"].eq("pyrocb").all()
    assert df["onset_utc"][0].isoformat() == "2024-07-20T22:00:00+00:00"


def test_load_string_booleans(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text(SAMPLE.replace("True", "yes").replace("False", "0"))
    df = events.load_events(p)
    assert df["kept"].to_list() == [True, True, True, False, False]


def test_bad_boolean(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text(SAMPLE.replace(",False,True", ",maybe,True"))
    with pytest.raises(events.EventTableError, match="non-boolean"):
        events.load_events(p)


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


def test_parquet_roundtrip(table, tmp_path):
    p = tmp_path / "events.parquet"
    events.load_events(table).write_parquet(p)
    df = events.load_events(p)
    assert df.schema["onset_utc"] == pl.Datetime("us", "UTC")
    assert df.height == 5


def test_add_windows(table):
    df = events.add_windows(events.load_events(table), pre_days=1, post_days=10)
    assert df.schema["window_start"] == pl.Datetime("us", "UTC")
    assert df.schema["window_end"] == pl.Datetime("us", "UTC")
    first = df.row(0, named=True)
    assert (first["onset_utc"] - first["window_start"]).days == 1
    assert (first["window_end"] - first["onset_utc"]).days == 10


def test_coverage_counts(table):
    df = events.add_windows(events.load_events(table), pre_days=1, post_days=10)
    cov = events.coverage(df, {"ppe": ("2024-08-01", "2025-08-01")}, by=None)
    row = cov.row(0, named=True)
    assert row["group"] == "all"
    assert row["n_events"] == 5
    assert row["n_kept"] == 3
    assert row["onset_in"] == 3
    assert row["onset_in_kept"] == 1
    assert row["window_full"] == 3
    # EV_B (Jul 28 + 10 d) reaches into August; EV_A (Jul 20 + 10 d) does not.
    assert row["window_any"] == 4
    assert row["window_any_kept"] == 2


def test_coverage_by_region(table):
    cov = events.coverage(events.load_events(table), {"ppe": ("2024-08-01", None)})
    rows = {r["region"]: r for r in cov.iter_rows(named=True)}
    assert set(rows) == {"all", "Alaska", "Canada", "Northern Asia"}
    assert rows["all"]["n_events"] == sum(rows[k]["n_events"] for k in rows if k != "all")
    assert rows["Canada"]["onset_in"] == 0
    assert rows["Canada"]["window_any"] == 1
    assert rows["Northern Asia"]["n_kept"] == 1
    assert rows["Alaska"]["onset_in"] == 1
    assert rows["Alaska"]["onset_in_kept"] == 0


def test_cli_coverage(table, capsys):
    assert events.main(["coverage", str(table)]) == 0
    out = capsys.readouterr().out
    assert "ppe_pd2024_3h" in out
    assert "Alaska" in out


@pytest.mark.parametrize("suffix", [".parquet", ".csv"])
def test_cli_windows(table, tmp_path, suffix):
    out = tmp_path / f"w{suffix}"
    assert events.main(["windows", str(table), "-o", str(out)]) == 0
    if suffix == ".parquet":
        w = pl.read_parquet(out)
        assert w.schema["window_start"] == pl.Datetime("us", "UTC")
    else:
        w = pl.read_csv(out)
    assert w.height == 3
    assert {"window_start", "window_end", "kept"} <= set(w.columns)


def test_cli_windows_all(table, tmp_path):
    out = tmp_path / "w.parquet"
    assert events.main(["windows", str(table), "-o", str(out), "--all"]) == 0
    w = pl.read_parquet(out)
    assert w.height == 5
    assert w["kept"].to_list() == [True, True, True, False, False]
