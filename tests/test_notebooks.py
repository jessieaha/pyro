"""Committed notebooks must be output-free (outputs would publish restricted,
event-keyed data in this public repository) and must run on synthetic inputs."""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NOTEBOOKS = sorted((REPO / "notebooks").glob("*.ipynb"))


@pytest.mark.parametrize("nb", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_has_no_outputs(nb):
    cells = json.loads(nb.read_text())["cells"]
    for c in cells:
        if c["cell_type"] == "code":
            assert c.get("outputs") == [], f"{nb.name}: clear outputs before committing"
            assert c.get("execution_count") is None, f"{nb.name}: clear execution counts"


def test_event_comparison_notebook_runs(compare_match_dir, tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import polars as pl

    from pyro import regional

    so = pl.read_parquet(compare_match_dir / "superobs_echam_3h.parquet").with_columns(
        pl.Series("region", ["Canada"] * 3),
        pl.Series("category", ["background", "pyrocb", "pyrocb"]),
    )
    model = pl.read_parquet(compare_match_dir / "model_echam_3h.parquet")
    st, mem = regional.evaluate(so, model, "echam_3h")
    rdir = tmp_path / "regional"
    rdir.mkdir()
    st.write_parquet(rdir / "stats.parquet")
    regional.summarize_members(mem).write_parquet(rdir / "member_summary.parquet")
    monkeypatch.setenv("PYRO_REGIONAL_DIR", str(rdir))

    out = tmp_path / "out"
    monkeypatch.setenv("PYRO_MATCH_DIR", str(compare_match_dir))
    monkeypatch.setenv("PYRO_COMPARE_OUT", str(out))
    monkeypatch.chdir(tmp_path)
    cells = json.loads((REPO / "notebooks" / "event_comparison.ipynb").read_text())["cells"]
    ns = {"display": lambda *a, **k: None}
    for c in cells:
        if c["cell_type"] == "code":
            exec(compile("".join(c["source"]), "event_comparison.ipynb", "exec"), ns)
    assert (out / "figures" / "EV1.png").exists()
    assert (out / "metrics.csv").exists()
