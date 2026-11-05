# Storage options and the sync pipeline

## 1. What has to be stored

| Data | Size (step 1) | Restricted? | Notes |
|---|---|---|---|
| Raw PPE / MM PPE output | 6.5 TB (3-hourly) + monthly | — | **Never moves.** Stays in Snellius project space |
| Model event subsets | ~20–100 GB | no (unpublished model output; keep private until release) | netCDF |
| Satellite extracts | ~10–50 GB | no (public products) | parquet; raw granules can be downloaded again |
| Event tables, lookalike controls | < 100 MB | **yes** | From the fire side, unpublished |
| Collocation and aggregates | ~10–100 GB | **yes** (keyed by event) | parquet, partitioned |
| ICON-HAM PPE 2024–25 (monthly, all variables) | ≈ 8 GB as parquet (22 GB netCDF) | no | source folder is temporary |
| GLD360 lightning, raw | — | **licensed** | Never synced by this tool (`never_sync`) |

## 2. Comparison

| | (a) University Google Drive via rclone | (b) SURF Research Drive (WebDAV) | (c) SURF Object Store (S3) / dCache | (d) Snellius project space over SSH | (e) Commercial S3 |
|---|---|---|---|---|---|
| Capacity / cost | **1 TB on Jessie's VU account**, no extra cost | Project folder quota through the institution's Research Drive contract | Contract via SURF (institution or grant). TB scale is routine | The project's grant quota; time-limited to the project | Pay per GB-month plus egress (AWS ~$0.02/GB-mo + egress; R2 no egress; B2 cheaper). Check current prices |
| Throughput | Moderate. API limits: ~750 GB/day upload per user, a few file creations per second | Moderate; per-file overhead | High and parallel; no per-file penalty | Highest from Snellius; SFTP from outside is moderate | High |
| Many files / large parquet | Poor for thousands of small files (slow listing/creation). Fine for tens–hundreds of 50–700 MB parquet files; range reads work through `rclone serve s3` (`pyro-query`) | Poor to moderate | **Good.** Range reads (`s3fs`, DuckDB, `pyarrow.dataset`) let you query without downloading | Good on Snellius | Good |
| Access for collaborators | Easy with Google accounts | Easy, incl. external guests | Per-user keys or bucket policies | Needs a Snellius account in the project | Needs IAM setup |
| Restricted-data fitness | Acceptable on the institution-managed VU account if nothing is link-shared (check the VU data classification policy) | Good (per-folder ACLs, EU-hosted) | Good (private bucket, EU-hosted, SURF contract) | Good (project ACLs) | Only with a contract and EU region |
| Checksums (rclone) | MD5 | SHA1/MD5 (`owncloud` vendor) | MD5 (rclone stores it for multipart uploads) | md5sum over SSH (slow for TB) | MD5 |
| DVC remote | `gdrive` (needs your own OAuth client) | `webdav` | `s3` (native) | `ssh` | `s3` |
| Durability | Google redundancy | Versioning and trash bin (check the VU contract for backup) | Replicated Ceph. dCache can be tape-backed | **No backup**; deleted after the project ends | Provider SLA |
| Verdict | **Primary shared store for now** (1 TB, already paid for) | **Fallback** and easy exchange | Upgrade path beyond ~1 TB or for heavy in-place queries | Compute-side working copy | Only if SURF is unavailable, open products only |

dCache at SURF (WebDAV/xrootd with macaroons, tape-backed) suits grid-scale archival of large immutable files better than this project's analysis-ready parquet. Its onboarding is also heavier than the Object Store's, so the Object Store wins here.

## 3. Recommendation

1. **Google Drive (VU account, 1 TB)** is the shared store for step 1: `pyro-sync` remote `gdrive` (rclone remote `pyrogdrive`) and the DVC default remote `gdrive`. Step-1 products (model subsets, satellite extracts, collocation, ICON-HAM parquet) fit well under 1 TB. Rules that make Drive work for parquet:
   - Few, large files: partition by month or event, aim for 50–700 MB per file, never one file per pixel or per granule.
   - Upload ≤ 750 GB/day (enforced by `--max-transfer=700G` on the remote); creations are rate-limited (`--tpslimit=8`).
   - Query in place with `pyro-query` / `pyro.store.served()` (below), or pull a partition with `pyro-sync pull … --subpath` and query locally.
   - Never use "anyone with the link" sharing for restricted folders; share with named VU/collaborator accounts only. GLD360 data never goes there (`never_sync`).
2. **Snellius** is where raw model output stays and all heavy steps run. Scratch-shared is the temporary working area (14-day purge, refreshed by `scripts/scratch_keepalive.sh`); anything worth keeping is pushed to Drive with `pyro-sync` from a `staging` job.
3. **SURF Object Store (S3)** remains the upgrade path once data exceed ~1 TB, when many collaborators query the same parquet in place, or if VU policy rules restricted data out of Drive. Nothing else changes: the same datasets, `pyro-sync push … -r surf_s3`, and the `surf` DVC remote.
4. **SURF Research Drive** stays the fallback for browsable exchange with the fire side.

Action for Jessie: link Drive once with `scripts/gdrive_setup.sh` (interactive; Google login in a laptop browser). The token stays in `~/.config/rclone/rclone.conf` on Snellius, not in the repo.

- The script prints one `rclone authorize "drive" "<blob>"` command to run on the laptop (rclone installed there) and reads the JSON token it returns, hidden; `--token-file FILE` reads it from a file instead. `scripts/gdrive_setup.sh status` says `linked`, `no_token` or `missing`.
- `empty token found - please run "rclone config reconnect pyrogdrive:"` means the remote was saved without a token. Recent rclone versions do that when `rclone config create` runs without a terminal; the earlier version of the script did exactly this. Rerunning `scripts/gdrive_setup.sh` repairs it. `pyro-sync` prints the same hint when a Drive transfer fails.
- rclone's shared Google OAuth client is being retired during 2026, and VU may block it. Create your own Desktop-app OAuth client (Google Cloud console, Drive API enabled, consent screen "Internal") and export `RCLONE_DRIVE_CLIENT_ID` / `RCLONE_DRIVE_CLIENT_SECRET` before running the script; the printed laptop command then uses the same client.
- Scope `drive.file` means rclone only sees what it created itself. A `pyro` folder made by hand in the Drive web UI stays invisible; let the script's `check` step create it.

### Data flow

```
ICON / ECHAM PPE (project space, read-only)
   └─ pyro-icon convert / model subsets ──► scratch-shared/pyro (working copy, keep-alive every 12 d)
SPEXone extracts ──► pyro-match ──► $PYRO_DATA_ROOT/derived/spexone_event_match (restricted)
   └─ pyro-sync push <dataset> -r gdrive --apply --slurm ──► Drive: pyro/<dataset>/   (checksummed copy)
   └─ dvc add + dvc push ──► Drive (DVC gdrive remote) or localstore → pyro-sync dvc_store → Drive
Laptop / ADA / Codespaces ──► pyro-query (in place) or pyro-sync pull / dvc pull
```

## 4. How the sync tool works

`pyro-sync` (`src/pyro/sync.py`, wrapper `scripts/sync.sh`) is a thin, config-driven layer over **rclone**:

- **Config:** `config/sync.toml` (committed, generic) defines *datasets*: local folders under `$PYRO_DATA_ROOT` with include/exclude rules and a `restricted` flag. It also defines *remotes*: an rclone remote name plus root, `public`, `restricted_ok` and `checksum`. Machine-specific paths come from `config/local.env` (gitignored).
- **Remotes are defined by environment variables** (`RCLONE_CONFIG_PYROS3_TYPE=s3`, …), so no rclone config file with secrets is needed. The tool never reads or prints credential values. `pyro-sync remotes` shows which remotes are configured.
- **Safe defaults:** dry run unless you pass `--apply`. `copy` never deletes. `--mirror` (rclone `sync`) moves anything it would delete into `<remote>/_trash/<dataset>/<timestamp>` and needs `--yes-delete` together with `--apply`.
- **Integrity:** `--checksum` comparisons by default, plus `pyro-sync check` (`rclone check --one-way`).
- **Policy guard:** restricted datasets only go to remotes that are private and `restricted_ok`, and `allowed_remotes` narrows this further. `never_sync` patterns (GLD360, lightning raw data, credential files) are excluded from every transfer in both directions.
- **Slurm mode:** `--slurm` writes an sbatch script for the `staging` partition and submits it; `--slurm-print` only shows it. Account and time come from `PYRO_SLURM_*`. Credentials reach the job only through the exported environment and are never written into the script.
- **Logs:** each run writes an rclone log to `$PYRO_DATA_ROOT/logs/sync/`.

```bash
cp config/local.env.example config/local.env      # edit paths (gitignored)
# export RCLONE_CONFIG_PYROS3_* and AWS_* from your secret store
scripts/sync.sh remotes
scripts/sync.sh datasets
scripts/sync.sh push model_subsets -r surf_s3                    # dry run: what would move
scripts/sync.sh push model_subsets -r surf_s3 --apply --slurm    # real transfer as a staging job
scripts/sync.sh check model_subsets -r surf_s3                   # verify checksums
scripts/sync.sh pull collocation -r surf_s3 --subpath obs/product=spexone_rtap --apply   # back to Snellius / a laptop
scripts/sync.sh push events -r public_release                    # refused: restricted dataset
scripts/sync.sh push icon_ppe -r gdrive --apply --slurm          # ICON-HAM parquet store -> Drive
```

### Querying parquet on Drive without downloading it

```bash
pyro-query model/icon_ham_ppe_24_25/values --remote pyrogdrive:pyro --schema
pyro-query model/icon_ham_ppe_24_25/values --remote pyrogdrive:pyro \
  --sql "SELECT time_label, member, avg(TAU_2D_550nm) AS aod FROM t WHERE lat > 50 GROUP BY 1, 2" -o aod.parquet
```

`pyro.store.served()` starts `rclone serve s3` on 127.0.0.1 with a random, per-process key (passed via the environment, so it is not visible in `ps`) and reads through `s3fs` + `pyarrow.dataset`, so polars only fetches the footers, row groups and columns a query needs. Latitude-sorted row groups make regional queries cheap. polars' own S3 reader is not used because `rclone serve s3` answers range requests with HTTP 200. For repeated heavy queries, pull the partitions once (`pyro-sync pull icon_ppe -r gdrive --subpath values/time_label=2024-08-01 --apply`) and use `pyro.store.scan()` locally.

From a laptop, Codespaces or VU ADA, the `snellius` remote (SFTP, ssh-agent key) pulls directly from project space: `scripts/sync.sh pull model_subsets -r snellius --apply`.

## 5. DVC for versioned snapshots

`pyro-sync` moves working directories. DVC pins **versions** of the datasets that analyses depend on, so a collaborator can check out a commit and run `dvc pull` to get exactly the matching data.

```bash
uv sync --extra dvc                       # or use the container
dvc add data/derived/model_subsets        # writes data/derived/model_subsets.dvc (committed)
git add data/derived/model_subsets.dvc data/derived/.gitignore && git commit -m "data: model subsets v1"
dvc push                                  # -> default remote `gdrive` (Google Drive folder)
# collaborator:
dvc pull data/derived/model_subsets.dvc
```

- Remotes are in `.dvc/config`: `gdrive` (default, Google Drive folder), `localstore` (a directory, e.g. on scratch, mirrored to Drive by `pyro-sync push dvc_store -r gdrive`), `surf` (S3), `researchdrive` (WebDAV) and `snellius` (SSH).
- `gdrive` setup: create a folder `pyro/dvc` on Drive and set `dvc remote modify --local gdrive url gdrive://<folder-id>`. Google blocks DVC's shared OAuth app for many institutions, so also set your own Desktop OAuth client: `dvc remote modify --local gdrive gdrive_client_id …` and `gdrive_client_secret …` (both go to the gitignored `.dvc/config.local`). If that is not possible under VU policy, use `dvc remote default localstore` and mirror the store with `pyro-sync`, which reuses the rclone login.
- DVC caches are content-addressed copies; keep `cache.dir` outside home quota for big data (`dvc cache dir --local /scratch-shared/$USER/pyro/dvc_cache`) and push before the 14-day purge window matters. Their URLs are placeholders; set the real ones with `dvc remote modify --local <name> url …`, which writes the gitignored `.dvc/config.local`. Credentials: `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` for `surf`, and `dvc remote modify --local researchdrive user/password` for WebDAV.
- **Restricted outputs** (collocation, aggregates, event tables) may only live in a private remote. Add `remote: surf` to their `.dvc` file, or use a separate private bucket. The `.dvc` pointer files hold only hashes and sizes, but their names are visible in this public repo, so keep event-specific names out of the paths.

## 6. Licensing and data-protection rules

- GLD360 lightning raw data (licensed) and unpublished fire-side event data **must never go to a public location**: not public buckets, not this public repo, not public DVC remotes. The sync tool enforces this with `restricted`/`never_sync`; reviewers should enforce it for everything else.
- The repo is public. Snellius paths, project and account names live only in `config/local.env`.
- Released products, once published, go to a `public = true` remote such as `public_release` or Zenodo, after a check by both sides.
