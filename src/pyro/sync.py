"""Config-driven rclone wrapper to move project data between Snellius and remotes.

Design rules:

* Dry run is the default; nothing is transferred without ``--apply``.
* ``copy`` (never deletes) is the default; ``--mirror`` switches to ``rclone sync``
  and moves anything it would delete into a dated ``_trash`` folder instead.
* Remotes are rclone remotes defined through ``RCLONE_CONFIG_<NAME>_*`` environment
  variables (or the user's own rclone config). This tool never reads, writes or
  prints credentials.
* Datasets marked ``restricted`` (unpublished event tables, licensed lightning data)
  can only go to remotes that are private *and* explicitly marked ``restricted_ok``.
  Patterns in ``never_sync`` are excluded from every transfer.

Run ``pyro-sync --help`` or see docs/storage_and_sync.md.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pyro.envfile import MissingVariable, expand, load_env_file

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "sync.toml"
DEFAULT_ENV_FILE = REPO_ROOT / "config" / "local.env"


class SyncError(RuntimeError):
    pass


class PolicyError(SyncError):
    pass


@dataclass(frozen=True)
class Remote:
    name: str
    rclone_remote: str
    root: str
    public: bool = False
    restricted_ok: bool = False
    checksum: bool = True
    description: str = ""
    extra_flags: tuple[str, ...] = ()
    setup: str = ""


@dataclass(frozen=True)
class Dataset:
    name: str
    path: str
    remote_subdir: str
    restricted: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    allowed_remotes: tuple[str, ...] = ()
    description: str = ""


@dataclass
class Config:
    data_root: str
    remotes: dict[str, Remote]
    datasets: dict[str, Dataset]
    never_sync: tuple[str, ...] = ()
    transfers: int = 8
    checkers: int = 16
    log_dir: str = "${PYRO_DATA_ROOT}/logs/sync"
    slurm: dict[str, str] = field(default_factory=dict)
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def x(self, value: str) -> str:
        try:
            return expand(value, self.environ)
        except MissingVariable as exc:
            raise SyncError(
                f"environment variable {exc.args[0]} is not set (see config/local.env.example)"
            ) from None

    def local_path(self, ds: Dataset) -> Path:
        p = Path(self.x(ds.path))
        return p if p.is_absolute() else Path(self.x(self.data_root)) / p

    def remote_path(self, ds: Dataset, rm: Remote) -> str:
        root = self.x(rm.root).rstrip("/")
        sub = ds.remote_subdir.strip("/")
        return f"{rm.rclone_remote}:{root}/{sub}" if root else f"{rm.rclone_remote}:{sub}"


def _tuple(v) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(i) for i in v)


def load_config(path: str | os.PathLike, environ: Mapping[str, str] | None = None) -> Config:
    raw = tomllib.loads(Path(path).read_text())
    environ = os.environ if environ is None else environ
    defaults = raw.get("defaults", {})
    remotes = {}
    for name, r in raw.get("remotes", {}).items():
        remotes[name] = Remote(
            name=name,
            rclone_remote=r["rclone_remote"],
            root=r.get("root", ""),
            public=bool(r.get("public", False)),
            restricted_ok=bool(r.get("restricted_ok", False)),
            checksum=bool(r.get("checksum", True)),
            description=r.get("description", ""),
            extra_flags=_tuple(r.get("extra_flags")),
            setup=r.get("setup", ""),
        )
    datasets = {}
    for name, d in raw.get("datasets", {}).items():
        unknown = set(_tuple(d.get("allowed_remotes"))) - set(remotes)
        if unknown:
            raise SyncError(f"dataset {name}: allowed_remotes not defined: {sorted(unknown)}")
        datasets[name] = Dataset(
            name=name,
            path=d["path"],
            remote_subdir=d.get("remote_subdir", d["path"]),
            restricted=bool(d.get("restricted", False)),
            include=_tuple(d.get("include")),
            exclude=_tuple(d.get("exclude")),
            allowed_remotes=_tuple(d.get("allowed_remotes")),
            description=d.get("description", ""),
        )
    return Config(
        data_root=raw.get("data_root", "${PYRO_DATA_ROOT}"),
        remotes=remotes,
        datasets=datasets,
        never_sync=_tuple(raw.get("never_sync")),
        transfers=int(defaults.get("transfers", 8)),
        checkers=int(defaults.get("checkers", 16)),
        log_dir=defaults.get("log_dir", "${PYRO_DATA_ROOT}/logs/sync"),
        slurm={k: str(v) for k, v in raw.get("slurm", {}).items()},
        environ=environ,
    )


def check_policy(ds: Dataset, rm: Remote) -> None:
    if ds.allowed_remotes and rm.name not in ds.allowed_remotes:
        raise PolicyError(
            f"dataset '{ds.name}' may only sync with {list(ds.allowed_remotes)}, not '{rm.name}'"
        )
    if ds.restricted and rm.public:
        raise PolicyError(f"dataset '{ds.name}' is restricted; remote '{rm.name}' is public")
    if ds.restricted and not rm.restricted_ok:
        raise PolicyError(
            f"dataset '{ds.name}' is restricted; remote '{rm.name}' is not marked restricted_ok"
        )


def filter_flags(
    cfg: Config, ds: Dataset, include: Sequence[str] = (), exclude: Sequence[str] = ()
) -> list[str]:
    """rclone --filter rules: excludes first (never_sync always wins), then includes."""
    flags: list[str] = []
    for pat in (*cfg.never_sync, *ds.exclude, *exclude):
        flags += ["--filter", f"- {pat}"]
    includes = (*ds.include, *include)
    for pat in includes:
        flags += ["--filter", f"+ {pat}"]
    if includes:
        flags += ["--filter", "- **"]
    return flags


def rclone_bin(environ: Mapping[str, str]) -> str:
    return environ.get("PYRO_SYNC_RCLONE") or "rclone"


def build_transfer(
    cfg: Config,
    ds: Dataset,
    rm: Remote,
    direction: str,
    *,
    apply: bool = False,
    mirror: bool = False,
    subpath: str = "",
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    transfers: int | None = None,
    bwlimit: str | None = None,
    log_file: Path | None = None,
    timestamp: str | None = None,
) -> list[str]:
    if direction not in ("push", "pull", "check"):
        raise SyncError(f"unknown direction {direction!r}")
    check_policy(ds, rm)
    local = cfg.local_path(ds)
    remote = cfg.remote_path(ds, rm)
    sub = subpath.strip("/")
    if sub:
        local = local / sub
        remote = f"{remote}/{sub}"

    if direction == "check":
        src, dst = str(local), remote
        cmd = [rclone_bin(cfg.environ), "check", src, dst, "--one-way"]
        if not rm.checksum:
            cmd.append("--size-only")
        cmd += ["--checkers", str(cfg.checkers)]
    else:
        src, dst = (str(local), remote) if direction == "push" else (remote, str(local))
        verb = "sync" if mirror else "copy"
        cmd = [rclone_bin(cfg.environ), verb, src, dst]
        cmd += ["--transfers", str(transfers or cfg.transfers), "--checkers", str(cfg.checkers)]
        if rm.checksum:
            cmd.append("--checksum")
        if mirror:
            ts = timestamp or _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            if direction == "push":
                root = cfg.x(rm.root).rstrip("/")
                prefix = f"{root}/" if root else ""
                backup = f"{rm.rclone_remote}:{prefix}_trash/{ds.remote_subdir.strip('/')}/{ts}"
            else:
                backup = str(Path(cfg.x(cfg.data_root)) / "_trash" / ds.name / ts)
            cmd += ["--backup-dir", backup]
        if bwlimit:
            cmd += ["--bwlimit", bwlimit]
        if not apply:
            cmd.append("--dry-run")
        cmd += list(rm.extra_flags)

    cmd += filter_flags(cfg, ds, include, exclude)
    cmd += ["--stats", "1m", "--stats-one-line", "--stats-log-level", "NOTICE"]
    if log_file is not None:
        cmd += ["--log-file", str(log_file), "--log-level", "INFO"]
    return cmd


def remote_configured(rm: Remote, environ: Mapping[str, str]) -> bool:
    env_key = f"RCLONE_CONFIG_{rm.rclone_remote.upper()}_TYPE"
    if environ.get(env_key):
        return True
    exe = shutil.which(rclone_bin(environ))
    if not exe:
        return False
    out = subprocess.run(
        [exe, "listremotes"], capture_output=True, text=True, env=dict(environ), check=False
    )
    return f"{rm.rclone_remote}:" in out.stdout.split()


def render_sbatch(cfg: Config, job_name: str, argv: Sequence[str], log_dir: Path) -> str:
    """Slurm script that re-runs this CLI (without --slurm) on a transfer node.

    Credentials are not written into the script: sbatch exports the submitting
    environment by default, so RCLONE_CONFIG_* variables reach the job via the
    environment only.
    """
    s = {
        "partition": "${PYRO_SLURM_PARTITION:-staging}",
        "time": "${PYRO_SLURM_TIME:-04:00:00}",
        "cpus": "${PYRO_SLURM_CPUS:-4}",
        "account": "${PYRO_SLURM_ACCOUNT:-}",
        **cfg.slurm,
    }
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --partition={cfg.x(s['partition'])}",
        f"#SBATCH --time={cfg.x(s['time'])}",
        "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={cfg.x(s['cpus'])}",
        f"#SBATCH --output={log_dir}/slurm-%x-%j.out",
    ]
    account = cfg.x(s["account"])
    if account:
        lines.append(f"#SBATCH --account={account}")
    launcher = shlex.split(cfg.environ.get("PYRO_SYNC_LAUNCHER", ""))
    run = [*launcher, sys.executable, "-m", "pyro.sync", *argv]
    lines += [
        "",
        "set -euo pipefail",
        f"cd {shlex.quote(str(Path.cwd()))}",
        "exec " + shlex.join(run),
        "",
    ]
    return "\n".join(lines)


def _strip_slurm_flags(argv: Sequence[str]) -> list[str]:
    return [a for a in argv if a not in ("--slurm", "--slurm-print")]


def _cmd_remotes(cfg: Config, _args) -> int:
    for rm in cfg.remotes.values():
        ok = remote_configured(rm, cfg.environ)
        if rm.public:
            vis = "public"
        else:
            vis = "private,restricted_ok" if rm.restricted_ok else "private"
        state = "configured" if ok else "NOT configured"
        print(f"{rm.name:16s} {rm.rclone_remote + ':':16s} {vis:22s} {state:15s} {rm.description}")
    return 0


def _cmd_datasets(cfg: Config, _args) -> int:
    for ds in cfg.datasets.values():
        try:
            lp = cfg.local_path(ds)
            where = f"{lp} ({'exists' if lp.exists() else 'missing'})"
        except SyncError as exc:
            where = f"<{exc}>"
        tag = "restricted" if ds.restricted else "open"
        print(f"{ds.name:22s} {tag:10s} {where}")
        if ds.description:
            print(f"{'':33s}{ds.description}")
    return 0


def _cmd_transfer(cfg: Config, args, argv: Sequence[str]) -> int:
    if args.dataset not in cfg.datasets:
        raise SyncError(f"unknown dataset {args.dataset!r}; choose from {sorted(cfg.datasets)}")
    if args.remote not in cfg.remotes:
        raise SyncError(f"unknown remote {args.remote!r}; choose from {sorted(cfg.remotes)}")
    ds, rm = cfg.datasets[args.dataset], cfg.remotes[args.remote]
    check_policy(ds, rm)
    mirror = getattr(args, "mirror", False)
    apply = getattr(args, "apply", False)
    if mirror and apply and not args.yes_delete:
        raise SyncError("--mirror --apply deletes files on the destination; add --yes-delete")

    log_dir = Path(cfg.x(cfg.log_dir))
    if args.slurm or args.slurm_print:
        log_dir.mkdir(parents=True, exist_ok=True)
        job = f"pyro-sync-{args.command}-{ds.name}"
        script = render_sbatch(cfg, job, _strip_slurm_flags(argv), log_dir)
        if args.slurm_print:
            print(script)
            return 0
        path = log_dir / f"{job}-{_dt.datetime.now():%Y%m%dT%H%M%S}.sbatch"
        path.write_text(script)
        print(f"wrote {path}")
        return subprocess.run(["sbatch", str(path)], check=False).returncode

    log_file = None
    if not args.no_log:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{_dt.datetime.now():%Y%m%dT%H%M%S}"
        log_file = log_dir / f"{args.command}-{ds.name}-{rm.name}-{stamp}.log"

    if args.command == "pull":
        cfg.local_path(ds).mkdir(parents=True, exist_ok=True)
    elif not cfg.local_path(ds).exists():
        raise SyncError(f"local path for '{ds.name}' does not exist: {cfg.local_path(ds)}")

    cmd = build_transfer(
        cfg,
        ds,
        rm,
        args.command,
        apply=apply,
        mirror=mirror,
        subpath=args.subpath,
        include=args.include,
        exclude=args.exclude,
        transfers=getattr(args, "transfers", None),
        bwlimit=getattr(args, "bwlimit", None),
        log_file=log_file,
    )
    if not shutil.which(cmd[0]):
        raise SyncError(f"rclone not found ({cmd[0]}); install it or set PYRO_SYNC_RCLONE")
    if not remote_configured(rm, cfg.environ):
        how = rm.setup or (
            f"export RCLONE_CONFIG_{rm.rclone_remote.upper()}_TYPE and friends "
            "(see config/remotes.env.example)"
        )
        raise SyncError(f"rclone remote '{rm.rclone_remote}:' is not configured; {how}")
    mode = "check" if args.command == "check" else ("APPLY" if apply else "dry-run")
    print(f"[{mode}] {shlex.join(cmd)}", flush=True)
    rc = subprocess.run(cmd, env=dict(cfg.environ), check=False).returncode
    if log_file is not None:
        print(f"log: {log_file}")
    if rc != 0 and rm.setup:
        print(
            f"rclone exited with {rc}. If it reported a login or token problem "
            f"for '{rm.rclone_remote}:', run: {rm.setup}",
            file=sys.stderr,
        )
    if args.command != "check" and not apply and rc == 0:
        print("dry run only; re-run with --apply to transfer")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pyro-sync", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default=os.environ.get("PYRO_SYNC_CONFIG", str(DEFAULT_CONFIG)))
    p.add_argument(
        "--env-file",
        default=os.environ.get("PYRO_SYNC_ENV_FILE", str(DEFAULT_ENV_FILE)),
        help="KEY=VALUE file with non-secret paths (default: config/local.env if present)",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("remotes", help="list remotes and whether rclone knows them")
    sub.add_parser("datasets", help="list datasets and their local paths")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("dataset")
        sp.add_argument("-r", "--remote", required=True)
        sp.add_argument("--subpath", default="", help="restrict to a sub-folder of the dataset")
        sp.add_argument("--include", action="append", default=[], help="extra rclone include")
        sp.add_argument("--exclude", action="append", default=[], help="extra rclone exclude")
        sp.add_argument("--slurm", action="store_true", help="submit as a Slurm job")
        sp.add_argument("--slurm-print", action="store_true", help="print the sbatch script")
        sp.add_argument("--no-log", action="store_true", help="do not write an rclone log file")

    for name, helptext in (("push", "local -> remote"), ("pull", "remote -> local")):
        sp = sub.add_parser(name, help=helptext)
        common(sp)
        sp.add_argument("--apply", action="store_true", help="actually transfer (default: dry run)")
        sp.add_argument("--mirror", action="store_true", help="rclone sync (deletions to _trash)")
        sp.add_argument("--yes-delete", action="store_true", help="confirm --mirror --apply")
        sp.add_argument("--transfers", type=int)
        sp.add_argument("--bwlimit")
    common(sub.add_parser("check", help="verify local files are present and identical remotely"))
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    load_env_file(args.env_file)
    try:
        cfg = load_config(args.config)
        if args.command == "remotes":
            return _cmd_remotes(cfg, args)
        if args.command == "datasets":
            return _cmd_datasets(cfg, args)
        return _cmd_transfer(cfg, args, argv)
    except SyncError as exc:
        print(f"pyro-sync: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
