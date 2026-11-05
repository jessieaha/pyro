import datetime as dt

import polars as pl
import pytest

ONSET = dt.datetime(2024, 8, 10, 0, tzinfo=dt.UTC)


def _superobs(times, ds, ppe_source, resolution):
    n = len(times)
    return pl.DataFrame(
        {
            "pcb_id": ["EV1"] * n,
            "region": ["Canada"] * n,
            "kept": [True] * n,
            "t_idx": list(range(n)),
            "cell": [100 + i for i in range(n)],
            "model_time": times,
            "model_lat": [60.0] * n,
            "model_lon": [-120.0] * n,
            "n_pix": [10] * n,
            "obs_time": times,
            "dt_hours": [0.0] * n,
            "dist_km": [100.0] * n,
            "surface": ["land"] * n,
            "o_aod550": [0.2, 1.0, 0.4][:n],
            "o_aod550_std": [0.01] * n,
            "o_aod550_unc": [0.02] * n,
            "o_ssa550": [0.95, 0.9, 0.93][:n],
            "o_ae440_670": [1.5, 1.6, 1.7][:n],
            "o_aaod550": [0.01, 0.1, 0.028][:n],
            "dataset": [ds] * n,
            "ppe_source": [ppe_source] * n,
            "resolution": [resolution] * n,
        }
    ).with_columns(pl.col("model_time", "obs_time").cast(pl.Datetime("us", "UTC")))


def _model(n_points, members, control, scale):
    rows = []
    for p in range(n_points):
        for m in members:
            aod = scale * (p + 1) * (1 + 0.1 * m)
            rows.append(
                {
                    "pcb_id": "EV1",
                    "point_id": p,
                    "member": m,
                    "is_control": control and m == 0,
                    "od550aer": aod,
                    "abs550aer": aod * 0.1,
                    "ssa550aer": 0.9,
                    "ae440_670": 1.8,
                    "ae550_865": 1.7,
                }
            )
    return pl.DataFrame(rows).with_columns(
        pl.col("point_id").cast(pl.Int64), pl.col("member").cast(pl.Int32)
    )


@pytest.fixture
def compare_match_dir(tmp_path):
    """Synthetic pyro-match output: event EV1 seen by a 3-hourly source with control
    (11 members, 3 super-obs) and a monthly source without control or AE (5 members,
    1 super-obs); event EV2 without any collocation."""
    d = tmp_path / "match"
    d.mkdir()
    t3 = [ONSET + dt.timedelta(hours=h) for h in (-12, 24, 48)]
    _superobs(t3, "echam_3h", "ybhatti", "3h").write_parquet(d / "superobs_echam_3h.parquet")
    _model(3, range(11), True, 0.2).write_parquet(d / "model_echam_3h.parquet")
    tm = [ONSET + dt.timedelta(days=3)]
    _superobs(tm, "icon_mon", "hjia", "1mon").write_parquet(d / "superobs_icon_mon.parquet")
    _model(1, range(5), False, 0.3).drop("ae440_670").write_parquet(d / "model_icon_mon.parquet")
    pl.DataFrame(
        {
            "pcb_id": ["EV1", "EV2"],
            "region": ["Canada", "Alaska"],
            "kept": [True, False],
            "onset_utc": [ONSET, dt.datetime(2024, 6, 1, tzinfo=dt.UTC)],
            "lat_c": [60.0, 64.0],
            "lon_c": [-120.0, -150.0],
            "n_pix_500km": [100, 0],
        }
    ).with_columns(pl.col("onset_utc").cast(pl.Datetime("us", "UTC"))).write_parquet(
        d / "summary.parquet"
    )
    pl.DataFrame(
        {
            "pcb_id": ["EV1"] * 3,
            "lat": [59.0, 60.0, 61.0],
            "lon": [-121.0, 239.0, -119.0],
            "aot_550nm": [0.1, 0.5, 1.2],
        }
    ).write_parquet(d / "pixels.parquet")
    return d
