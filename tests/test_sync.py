import shutil
import textwrap
from pathlib import Path

import pytest

from pyro import sync

REPO = Path(__file__).resolve().parents[1]

CONFIG = """
data_root = "${PYRO_DATA_ROOT}"
never_sync = ["*GLD360*", "*.env"]

[defaults]
transfers = 2
checkers = 2
log_dir = "${PYRO_DATA_ROOT}/logs"

[remotes.priv]
rclone_remote = "pyrotest"
root = "${REMOTE_ROOT}"
restricted_ok = true

[remotes.plain]
rclone_remote = "pyrotest"
root = "${REMOTE_ROOT}/plain"

[remotes.pub]
rclone_remote = "pyrotest"
root = "${REMOTE_ROOT}/pub"
public = true

[datasets.events]
path = "events"
restricted = true
include = ["*.csv"]

[datasets.derived]
path = "derived"
include = ["**.parquet", "**.nc"]

[datasets.only_priv]
path = "derived"
allowed_remotes = ["priv"]
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    remote = tmp_path / "remote"
    data.mkdir()
    remote.mkdir()
    monkeypatch.setenv("PYRO_DATA_ROOT", str(data))
    monkeypatch.setenv("REMOTE_ROOT", str(remote))
    monkeypatch.setenv("RCLONE_CONFIG_PYROTEST_TYPE", "local")
    monkeypatch.setenv("RCLONE_CONFIG_PYROTEST_PASS", "do-not-leak-me")
    cfg_path = tmp_path / "sync.toml"
    cfg_path.write_text(CONFIG)
    return {"data": data, "remote": remote, "config": cfg_path}


def _cfg(env):
    return sync.load_config(env["config"])


def test_restricted_never_to_public(env):
    cfg = _cfg(env)
    with pytest.raises(sync.PolicyError, match="public"):
        sync.check_policy(cfg.datasets["events"], cfg.remotes["pub"])


def test_restricted_needs_restricted_ok(env):
    cfg = _cfg(env)
    with pytest.raises(sync.PolicyError, match="restricted_ok"):
        sync.check_policy(cfg.datasets["events"], cfg.remotes["plain"])
    sync.check_policy(cfg.datasets["events"], cfg.remotes["priv"])


def test_allowed_remotes(env):
    cfg = _cfg(env)
    with pytest.raises(sync.PolicyError, match="only sync"):
        sync.check_policy(cfg.datasets["only_priv"], cfg.remotes["plain"])


def test_unknown_allowed_remote_rejected(env, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(CONFIG + '\n[datasets.x]\npath = "x"\nallowed_remotes = ["nope"]\n')
    with pytest.raises(sync.SyncError, match="nope"):
        sync.load_config(bad)


def test_push_is_dry_run_copy_by_default(env):
    cfg = _cfg(env)
    cmd = sync.build_transfer(cfg, cfg.datasets["derived"], cfg.remotes["plain"], "push")
    assert cmd[1] == "copy"
    assert "--dry-run" in cmd
    assert "--checksum" in cmd
    assert cmd[2] == str(env["data"] / "derived")
    assert cmd[3] == f"pyrotest:{env['remote']}/plain/derived"
    filters = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"]
    assert filters == ["- *GLD360*", "- *.env", "+ **.parquet", "+ **.nc", "- **"]


def test_apply_and_pull_direction(env):
    cfg = _cfg(env)
    cmd = sync.build_transfer(
        cfg, cfg.datasets["derived"], cfg.remotes["plain"], "pull", apply=True, subpath="a/b"
    )
    assert "--dry-run" not in cmd
    assert cmd[2].endswith("/plain/derived/a/b")
    assert cmd[3] == str(env["data"] / "derived" / "a" / "b")


def test_mirror_uses_backup_dir(env):
    cfg = _cfg(env)
    cmd = sync.build_transfer(
        cfg, cfg.datasets["derived"], cfg.remotes["plain"], "push", mirror=True, timestamp="T0"
    )
    assert cmd[1] == "sync"
    assert cmd[cmd.index("--backup-dir") + 1] == f"pyrotest:{env['remote']}/plain/_trash/derived/T0"


def test_missing_variable_is_reported(env, monkeypatch):
    monkeypatch.delenv("REMOTE_ROOT")
    cfg = _cfg(env)
    with pytest.raises(sync.SyncError, match="REMOTE_ROOT"):
        sync.build_transfer(cfg, cfg.datasets["derived"], cfg.remotes["plain"], "push")


def test_mirror_apply_requires_confirmation(env):
    (env["data"] / "derived").mkdir()
    rc = sync.main(
        [
            "--config",
            str(env["config"]),
            "--env-file",
            "/nonexistent",
            "push",
            "derived",
            "-r",
            "plain",
            "--mirror",
            "--apply",
        ]
    )
    assert rc == 2


def test_slurm_script_has_no_secrets(env, capsys, monkeypatch):
    monkeypatch.setenv("PYRO_SLURM_ACCOUNT", "acct123")
    (env["data"] / "derived").mkdir()
    rc = sync.main(
        [
            "--config",
            str(env["config"]),
            "--env-file",
            "/nonexistent",
            "push",
            "derived",
            "-r",
            "plain",
            "--apply",
            "--slurm-print",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "#SBATCH --partition=staging" in out
    assert "#SBATCH --account=acct123" in out
    assert "--slurm" not in out.split("exec", 1)[1]
    assert "do-not-leak-me" not in out


def test_repo_config_is_consistent(monkeypatch, tmp_path):
    monkeypatch.setenv("PYRO_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("PYRO_SNELLIUS_REMOTE_ROOT", "/remote/pyro_data")
    cfg = sync.load_config(REPO / "config" / "sync.toml")
    restricted = [d for d in cfg.datasets.values() if d.restricted]
    assert {d.name for d in restricted} >= {
        "events",
        "collocation",
        "aggregates",
        "spexone_event_match",
    }
    for ds in restricted:
        for name in ("commercial_s3", "public_release"):
            with pytest.raises(sync.PolicyError):
                sync.check_policy(ds, cfg.remotes[name])
        for name in ("surf_s3", "gdrive"):
            sync.check_policy(ds, cfg.remotes[name])
    assert not cfg.remotes["gdrive"].public
    assert any("GLD360" in p for p in cfg.never_sync)


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")
def test_end_to_end_local_remote(env):
    data, remote = env["data"], env["remote"]
    (data / "derived" / "ev1").mkdir(parents=True)
    (data / "derived" / "ev1" / "coloc.parquet").write_bytes(b"PAR1-data")
    (data / "derived" / "ev1" / "notes.txt").write_text("not included")
    (data / "derived" / "GLD360_strokes.parquet").write_bytes(b"licensed")
    base = ["--config", str(env["config"]), "--env-file", "/nonexistent"]

    assert sync.main([*base, "push", "derived", "-r", "plain"]) == 0
    assert not (remote / "plain").exists()

    assert sync.main([*base, "push", "derived", "-r", "plain", "--apply"]) == 0
    dest = remote / "plain" / "derived"
    assert (dest / "ev1" / "coloc.parquet").read_bytes() == b"PAR1-data"
    assert not (dest / "ev1" / "notes.txt").exists()
    assert not (dest / "GLD360_strokes.parquet").exists()

    assert sync.main([*base, "check", "derived", "-r", "plain"]) == 0
    (dest / "ev1" / "coloc.parquet").write_bytes(b"PAR1-diff")
    assert sync.main([*base, "check", "derived", "-r", "plain"]) != 0

    shutil.rmtree(data / "derived")
    assert sync.main([*base, "pull", "derived", "-r", "plain", "--apply"]) == 0
    assert (data / "derived" / "ev1" / "coloc.parquet").read_bytes() == b"PAR1-diff"

    assert sync.main([*base, "push", "events", "-r", "pub"]) == 2


def test_env_file_does_not_override(tmp_path, monkeypatch):
    from pyro.envfile import load_env_file

    f = tmp_path / "local.env"
    f.write_text(
        textwrap.dedent("""
        # comment
        export A=1
        B="two words"
        C=keep
    """)
    )
    environ = {"C": "existing"}
    assert load_env_file(f, environ) == ["A", "B"]
    assert environ == {"A": "1", "B": "two words", "C": "existing"}
