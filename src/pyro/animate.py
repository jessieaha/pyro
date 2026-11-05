"""Animate modelled AOD 550 nm and SSA 550 nm around a pyroCb event (plume view).

One frame per model output step (3-hourly for ``ybhatti_pd_3h``), shown for
``--seconds-per-frame`` (default 1 s). Two map panels with coastlines, country
and state/province borders, and the fire position.

    pyro-animate PCB_2024_0809_2320                          # +-1000 km, 24 steps
    pyro-animate PCB_2024_0809_2320 --steps 40 --start-hours -6
    pyro-animate PCB_2024_0809_2320 --lon-range -140 -100 --lat-range 50 70
    pyro-animate --center 61.5 -126 --start 2024-08-10T00 --steps 16 -o plume.gif
    pyro-animate PCB_... --member median                     # ensemble median (251 members)

Coverage: by default a box ``2 x --half-width-km`` (1000 km) wide in longitude
and latitude, centred on the event's ``lon_c``/``lat_c``; ``--lon-range`` and
``--lat-range`` override it. ``--member`` is ``control`` (default), ``median``,
``mean`` or a member number. Output: GIF (Pillow) or MP4 (needs ffmpeg), by
suffix; default ``$PYRO_DATA_ROOT/derived/animations/<event>_<member>.gif``
(restricted: named after the event).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from pyro import ppe

KM_PER_DEG = 111.2


@dataclass
class Fields:
    times: np.ndarray  # datetime64[s], (nt,)
    lat: np.ndarray  # (ny,) cell centres, north to south as on the grid
    lon: np.ndarray  # (nx,) continuous across the dateline
    aod: np.ndarray  # (nt, ny, nx)
    ssa: np.ndarray  # (nt, ny, nx)
    member: str


def event_box(lat_c: float, lon_c: float, half_width_km: float = 1000.0):
    """(lon_min, lon_max, lat_min, lat_max) of a box 2 x half_width_km wide."""
    dlat = half_width_km / KM_PER_DEG
    dlon = min(half_width_km / (KM_PER_DEG * max(math.cos(math.radians(lat_c)), 0.05)), 179.9)
    return (lon_c - dlon, lon_c + dlon, max(lat_c - dlat, -89.9), min(lat_c + dlat, 89.9))


def _select(grid: ppe.GaussianGrid, box):
    lon0, lon1, lat0, lat1 = box
    jj = np.where((grid.lat >= lat0) & (grid.lat <= lat1))[0]
    centre = 0.5 * (lon0 + lon1)
    rel = ((grid.lon - centre + 180.0) % 360.0) - 180.0
    half = 0.5 * (lon1 - lon0)
    keep = np.where(np.abs(rel) <= half)[0]
    order = keep[np.argsort(rel[keep])]
    if jj.size == 0 or order.size == 0:
        raise ValueError(f"box {box} contains no model cells")
    return jj, order, centre + rel[order]


def model_fields(
    src: ppe.Source,
    start: np.datetime64,
    steps: int,
    box,
    member: str | int = "control",
) -> Fields:
    """AOD and SSA (1 - AAOD/AOD) on the model grid inside ``box`` for ``steps``
    output steps starting at the step nearest ``start``."""
    if not isinstance(src.grid, ppe.GaussianGrid):
        raise TypeError("animation needs a lat-lon (T63) source such as ybhatti_pd_3h")
    t_idx, _ = src.time.locate(np.array([start], dtype="datetime64[s]"))
    if t_idx[0] < 0:
        raise ValueError(
            f"{start} is outside {src.key} ({src.time.stamps[0]} - {src.time.stamps[-1]})"
        )
    t = np.arange(t_idx[0], min(t_idx[0] + steps, len(src.time.stamps)))
    jj, ii, lon = _select(src.grid, box)
    cells = (jj[:, None] * src.grid.lon.size + ii[None, :]).ravel()
    tp = np.repeat(t, cells.size)
    cp = np.tile(cells, t.size)
    if member in ("median", "mean"):
        mem = src.members
    elif member == "control":
        if src.control_member is None:
            raise ValueError(f"{src.key} has no control member; use median, mean or a number")
        mem = np.array([src.control_member])
    else:
        mem = np.array([int(member)])
    od = src.read("od550aer", tp, cp, mem)
    ab = src.read("abs550aer", tp, cp, mem)
    with np.errstate(divide="ignore", invalid="ignore"):
        ssa = 1.0 - ab / od
    if member == "median":
        od, ssa = np.nanmedian(od, axis=0), np.nanmedian(ssa, axis=0)
    elif member == "mean":
        od, ssa = np.nanmean(od, axis=0), np.nanmean(ssa, axis=0)
    else:
        od, ssa = od[0], ssa[0]
    shape = (t.size, jj.size, ii.size)
    return Fields(
        times=src.time.stamps[t],
        lat=src.grid.lat[jj],
        lon=lon,
        aod=od.reshape(shape),
        ssa=np.where(np.isfinite(ssa), ssa, np.nan).reshape(shape),
        member=str(member),
    )


def _edges(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size == 1:
        return np.array([x[0] - 0.5, x[0] + 0.5])
    mid = 0.5 * (x[:-1] + x[1:])
    return np.concatenate([[x[0] - (mid[0] - x[0])], mid, [x[-1] + (x[-1] - mid[-1])]])


def animate(
    f: Fields,
    out: Path,
    *,
    fire: tuple[float, float] | None = None,
    onset: np.datetime64 | None = None,
    title: str = "",
    seconds_per_frame: float = 1.0,
    aod_range: tuple[float, float] | None = None,
    ssa_range: tuple[float, float] = (0.70, 1.0),
    borders: bool = True,
    dpi: int = 100,
) -> Path:
    """Write the animation (GIF or MP4 by suffix); one frame per model step."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import animation, colors

    lon_c = float(np.mean(f.lon))
    if borders:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature

        data_crs = ccrs.PlateCarree()
        proj = ccrs.PlateCarree(central_longitude=lon_c)
        kw = {"projection": proj}
    else:
        data_crs, kw = None, {}
    aspect = (np.ptp(f.lat) + 2) / max(np.ptp(f.lon) + 2, 1.0)
    height = min(max(6.2 * aspect, 2.6), 7.0) + 1.6
    fig, axes = plt.subplots(1, 2, figsize=(13, height), subplot_kw=kw, layout="constrained")

    vmax = aod_range[1] if aod_range else max(1.0, float(np.nanpercentile(f.aod, 99.5)))
    vmin = aod_range[0] if aod_range else 0.02
    norms = [colors.LogNorm(vmin, vmax), colors.Normalize(*ssa_range)]
    cmaps = ["magma_r", "RdYlBu"]
    labels = ["AOD 550 nm", "SSA 550 nm"]
    data = [f.aod, f.ssa]
    xe, ye = _edges(f.lon), _edges(f.lat)
    meshes = []
    for ax, arr, norm, cmap, lab in zip(axes, data, norms, cmaps, labels, strict=True):
        extra = {"transform": data_crs} if borders else {}
        m = ax.pcolormesh(xe, ye, arr[0], norm=norm, cmap=cmap, shading="flat", **extra)
        meshes.append(m)
        fig.colorbar(
            m, ax=ax, orientation="horizontal", shrink=0.8, pad=0.04, label=lab, extend="both"
        )
        if borders:
            ax.set_extent([xe[0], xe[-1], min(ye), max(ye)], crs=data_crs)
            ax.add_feature(cfeature.COASTLINE.with_scale("50m"), linewidth=0.7)
            ax.add_feature(cfeature.BORDERS.with_scale("50m"), linewidth=0.7)
            ax.add_feature(cfeature.STATES.with_scale("50m"), linewidth=0.3, edgecolor="0.4")
            gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="0.5", alpha=0.6)
            gl.top_labels = gl.right_labels = False
        else:
            ax.set_xlim(xe[0], xe[-1])
            ax.set_ylim(min(ye), max(ye))
        if fire is not None:
            extra = {"transform": data_crs} if borders else {}
            ax.plot(fire[1], fire[0], marker="*", color="cyan", ms=14, mec="k", **extra)
    sup = fig.suptitle("")

    def frame(k: int):
        for m, arr in zip(meshes, data, strict=True):
            m.set_array(arr[k].ravel())
        t = f.times[k]
        age = ""
        if onset is not None:
            hours = (t - onset).astype("timedelta64[m]").astype(float) / 60.0
            age = f"  (onset {hours:+.0f} h)"
        sup.set_text(f"{title}  {np.datetime_as_string(t, unit='m')} UTC{age}  [{f.member}]")
        return [*meshes, sup]

    frame(0)
    anim = animation.FuncAnimation(
        fig, frame, frames=len(f.times), interval=1000 * seconds_per_frame, blit=False
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fps = 1.0 / seconds_per_frame
    if out.suffix.lower() == ".mp4":
        writer = animation.FFMpegWriter(fps=fps)
    else:
        writer = animation.PillowWriter(fps=fps)
    anim.save(out, writer=writer, dpi=dpi)
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> int:
    from pyro.envfile import load_env_file
    from pyro.events import load_events

    p = argparse.ArgumentParser(
        prog="pyro-animate",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[2:]),
    )
    p.add_argument("event", nargs="?", help="pcb_id from the event table")
    p.add_argument(
        "--events",
        type=Path,
        default=None,
        help="event table (default $PYRO_DATA_ROOT/events/pcb_events_2024_boreal.csv)",
    )
    p.add_argument(
        "--center",
        nargs=2,
        type=float,
        metavar=("LAT", "LON"),
        help="instead of an event: box centre",
    )
    p.add_argument("--start", default=None, help="UTC start (default: event onset)")
    p.add_argument(
        "--start-hours",
        type=float,
        default=0.0,
        help="shift the start by this many hours (e.g. -6)",
    )
    p.add_argument(
        "--steps", type=int, default=24, help="number of model output steps (default 24)"
    )
    p.add_argument("--half-width-km", type=float, default=1000.0)
    p.add_argument("--lon-range", nargs=2, type=float, metavar=("W", "E"))
    p.add_argument("--lat-range", nargs=2, type=float, metavar=("S", "N"))
    p.add_argument("--member", default="control", help="control | median | mean | <number>")
    p.add_argument("--ppe", default="ybhatti_pd_3h")
    p.add_argument("--seconds-per-frame", type=float, default=1.0)
    p.add_argument("--aod-max", type=float, default=None)
    p.add_argument("--no-borders", action="store_true")
    p.add_argument("-o", "--out", type=Path, default=None)
    p.add_argument(
        "--env-file", default=str(Path(__file__).resolve().parents[2] / "config" / "local.env")
    )
    a = p.parse_args(argv)
    if Path(a.env_file).exists():
        load_env_file(a.env_file)
    root = Path(os.environ.get("PYRO_DATA_ROOT", "data"))

    onset = None
    if a.event:
        table = a.events or root / "events" / "pcb_events_2024_boreal.csv"
        ev = load_events(table)
        row = ev.filter(ev["pcb_id"] == a.event)
        if row.is_empty():
            p.error(f"{a.event} not in {table}")
        r = row.row(0, named=True)
        lat_c, lon_c = r["lat_c"], r["lon_c"]
        onset = np.datetime64(r["onset_utc"].replace(tzinfo=None), "s")
        title = f"{a.event} ({r['region']})"
    elif a.center:
        lat_c, lon_c = a.center
        title = f"{lat_c:.2f}N {lon_c:.2f}E"
        if not a.start:
            p.error("--start is required with --center")
    else:
        p.error("give an event id or --center LAT LON")
    start = np.datetime64(a.start, "s") if a.start else onset
    start = start + np.timedelta64(int(round(a.start_hours * 3600)), "s")

    box = list(event_box(lat_c, lon_c, a.half_width_km))
    if a.lon_range:
        box[0], box[1] = a.lon_range
    if a.lat_range:
        box[2], box[3] = a.lat_range
    member: str | int = a.member if a.member in ("control", "median", "mean") else int(a.member)
    src = ppe.open_source(a.ppe)
    if src.resolution != "3h":
        print(f"note: {a.ppe} is {src.resolution}, not 3-hourly", file=sys.stderr)
    f = model_fields(src, start, a.steps, tuple(box), member)
    name = a.event or f"{lat_c:.1f}_{lon_c:.1f}_{np.datetime_as_string(start, unit='h')}"
    out = a.out or root / "derived" / "animations" / f"{name}_{f.member}.gif"
    animate(
        f,
        out,
        fire=(lat_c, lon_c),
        onset=onset,
        title=title,
        seconds_per_frame=a.seconds_per_frame,
        aod_range=(0.02, a.aod_max) if a.aod_max else None,
        borders=not a.no_borders,
    )
    print(
        f"{len(f.times)} frames ({f.times[0]} - {f.times[-1]}), "
        f"{f.lat.size}x{f.lon.size} cells -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
