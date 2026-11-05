"""Query hive-partitioned parquet datasets locally or in place on an rclone remote.

Local (Snellius, after ``pyro-sync pull``)::

    lf = scan("/scratch-shared/me/pyro/icon_ham_ppe_24_25/values")

In place on Google Drive (or any rclone remote) without downloading whole files:
``served()`` starts ``rclone serve s3`` on 127.0.0.1 with a random access key, and
a pyarrow dataset over ``s3fs`` reads only the row groups / columns a query needs::

    with served("pyrogdrive:pyro") as srv:
        lf = scan(srv.path("model/icon_ham_ppe_24_25/values"), srv.filesystem)
        lf.filter(pl.col("lat") > 50).select("member", "TAU_2D_550nm").collect()

(polars' native S3 reader is not used here: ``rclone serve s3`` answers range
requests with status 200, which it rejects; ``s3fs`` accepts them.)

CLI: ``pyro-query SOURCE [--remote REMOTE:ROOT] --sql "SELECT ... FROM t ..."``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import polars as pl


def scan(source: str, filesystem: Any = None) -> pl.LazyFrame:
    """Lazily scan every parquet file under ``source``; hive partitions become columns.

    ``filesystem`` is an fsspec/pyarrow filesystem for remote sources (see ``served``).
    """
    if filesystem is None:
        src = str(source).rstrip("/")
        pattern = src if src.endswith(".parquet") else f"{src}/**/*.parquet"
        return pl.scan_parquet(pattern, hive_partitioning=True)
    import pyarrow.dataset as ds

    dset = ds.dataset(source, filesystem=filesystem, format="parquet", partitioning="hive")
    return pl.scan_pyarrow_dataset(dset)


@dataclass
class Served:
    endpoint: str
    bucket: str
    filesystem: Any = field(repr=False)
    process: subprocess.Popen = field(repr=False)

    def path(self, rel: str = "") -> str:
        """Dataset path for ``rel`` (relative to the served remote root)."""
        return "/".join(x for x in (self.bucket, rel.strip("/")) if x)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def served(
    remote: str, extra_flags: list[str] | None = None, timeout: float = 30.0
) -> Iterator[Served]:
    """Expose ``remote`` (e.g. ``pyrogdrive:pyro``) as a local, read-only S3 endpoint.

    rclone serves the children of a path as buckets, so the parent of ``remote`` is
    served and its last path element becomes the bucket. The key pair is random and
    only known to this process and the rclone child (login nodes are multi-user, so
    the endpoint must not be anonymous).
    """
    import s3fs

    if shutil.which("rclone") is None:
        raise RuntimeError("rclone not found on PATH")
    name, _, root = remote.rpartition(":")
    parent, _, bucket = root.strip("/").rpartition("/")
    base = f"{name}:{parent}" if name else ("/" if root.startswith("/") else "") + parent
    key, secret = secrets.token_hex(8), secrets.token_hex(16)
    port = _free_port()
    cmd = [
        "rclone",
        "serve",
        "s3",
        base,
        "--addr",
        f"127.0.0.1:{port}",
        "--read-only",
        "--vfs-cache-mode",
        "off",
        "--no-modtime",
        *(extra_flags or []),
    ]
    # Passed via the environment so the key does not show up in `ps` output.
    env = {**os.environ, "RCLONE_AUTH_KEY": f'"{key},{secret}"'}
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env
    )
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"rclone serve s3 exited: {proc.stderr.read()[-2000:]}")
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
            break
        if time.time() > deadline:
            proc.terminate()
            raise RuntimeError("rclone serve s3 did not start in time")
        time.sleep(0.2)
    fs = s3fs.S3FileSystem(
        key=key, secret=secret, client_kwargs={"endpoint_url": endpoint, "region_name": "us-east-1"}
    )
    try:
        yield Served(endpoint, bucket, fs, proc)
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pyro-query", description=__doc__.splitlines()[0])
    p.add_argument("source", help="local dir/file, or a path below --remote")
    p.add_argument("--remote", help="rclone remote root to serve, e.g. pyrogdrive:pyro")
    p.add_argument("--sql", default="SELECT * FROM t LIMIT 10", help="query on table `t`")
    p.add_argument("-o", "--output", help="write result to .parquet or .csv")
    p.add_argument("--schema", action="store_true", help="print the schema and exit")
    a = p.parse_args(argv)

    def run(lf: pl.LazyFrame) -> None:
        if a.schema:
            for k, v in lf.collect_schema().items():
                print(f"{k}: {v}")
            return
        df = pl.SQLContext(t=lf).execute(a.sql, eager=True)
        if a.output:
            (df.write_parquet if a.output.endswith(".parquet") else df.write_csv)(a.output)
        with pl.Config(tbl_rows=50, tbl_cols=-1):
            print(df)

    if a.remote:
        with served(a.remote) as srv:
            run(scan(srv.path(a.source), srv.filesystem))
    else:
        run(scan(a.source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
