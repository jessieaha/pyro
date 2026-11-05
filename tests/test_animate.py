import numpy as np
import pytest
from test_match import FakeT63

from pyro import animate


def test_event_box_is_about_2000_km():
    lon0, lon1, lat0, lat1 = animate.event_box(60.0, -120.0, 1000.0)
    assert (lat1 - lat0) * 111.2 == pytest.approx(2000, rel=1e-3)
    assert (lon1 - lon0) * 111.2 * np.cos(np.radians(60)) == pytest.approx(2000, rel=1e-3)


def test_model_fields_box_steps_and_members():
    src = FakeT63()
    start = np.datetime64("2024-08-10T22:00:00")
    f = animate.model_fields(src, start, 5, animate.event_box(60.0, -120.0), "control")
    assert f.times[0] == np.datetime64("2024-08-10T21:00:00")  # nearest 3-hourly step
    assert len(f.times) == 5 and np.all(np.diff(f.times) == np.timedelta64(3, "h"))
    assert f.aod.shape == (5, f.lat.size, f.lon.size)
    assert f.lat.min() >= 51 and f.lat.max() <= 69
    assert np.all(np.diff(f.lon) > 0) and f.lon.min() > -140 and f.lon.max() < -100
    assert np.allclose(f.aod, 0.1)  # control member 0
    assert np.allclose(f.ssa, 1 - 0.01 / 0.1)
    med = animate.model_fields(src, start, 2, animate.event_box(60.0, -120.0), "median")
    assert np.allclose(med.aod, 0.2)  # members 0..2 -> 0.1, 0.2, 0.3
    two = animate.model_fields(src, start, 2, animate.event_box(60.0, -120.0), 2)
    assert np.allclose(two.aod, 0.3)


def test_box_across_the_dateline():
    f = animate.model_fields(
        FakeT63(), np.datetime64("2024-08-11"), 1, animate.event_box(65.0, 179.0, 500), "control"
    )
    assert np.all(np.diff(f.lon) > 0) and f.lon.min() < 179 < f.lon.max()


def test_animation_one_second_per_step(tmp_path):
    from PIL import Image

    f = animate.model_fields(
        FakeT63(), np.datetime64("2024-08-11"), 3, animate.event_box(60.0, -120.0), "control"
    )
    out = animate.animate(
        f,
        tmp_path / "a.gif",
        fire=(60.0, -120.0),
        onset=np.datetime64("2024-08-10T22:00"),
        borders=False,
    )
    im = Image.open(out)
    assert im.n_frames == 3 and im.info["duration"] == 1000


def test_outside_period_and_coarse_grid_errors():
    src = FakeT63()
    with pytest.raises(ValueError, match="outside"):
        animate.model_fields(src, np.datetime64("2023-01-01"), 2, animate.event_box(60, -120))
