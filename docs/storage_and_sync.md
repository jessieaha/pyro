# Storage options and the sync pipeline

## 1. What has to be stored

| Data | Size (step 1) | Restricted? | Notes |
|---|---|---|---|
| Raw PPE / MM PPE output | 6.5 TB (3-hourly) + monthly | — | **Never moves.** Stays in Snellius project space |
| Model event subsets | ~20–100 GB | no (unpublished model output; keep private until release) | netCDF |
| Satellite extracts | ~10–50 GB | no (public products) | parquet; raw granules can be downloaded again |
| Event tables, lookalike controls | < 100 MB | **yes** | From the fire side, unpublished |
| Collocation and aggregates | ~10–100 GB | **yes** (keyed by event) | parquet, many files |
| GLD360 lightning, raw | — | **licensed** | Never synced by this tool (`never_sync`) |

## 2. Comparison

| | (a) University Google Drive via rclone | (b) SURF Research Drive (WebDAV) | (c) SURF Object Store (S3) / dCache | (d) Snellius project space over SSH | (e) Commercial S3 |
|---|---|---|---|---|---|
| Capacity / cost | Institutional pool. The per-user quota is set by the university admin | Project folder quota through the institution's Research Drive contract | Contract via SURF (institution or grant). TB scale is routine | The project's grant quota; time-limited to the project | Pay per GB-month plus egress (AWS ~$0.02/GB-mo + egress; R2 no egress; B2 cheaper). Check current prices |
| Throughput | Moderate. API limits: ~750 GB/day upload per user, a few file creations per second | Moderate; per-file overhead | High and parallel; no per-file penalty | Highest from Snellius; SFTP from outside is moderate | High |
| Many files / large parquet | **Poor.** Slow listing and creation, no useful range reads | Poor to moderate | **Good.** Range reads (`s3fs`, DuckDB, `pyarrow.dataset`) let you query without downloading | Good on Snellius | Good |
| Access for collaborators | Easy with Google accounts | Easy, incl. external guests | Per-user keys or bucket policies | Needs a Snellius account in the project | Needs IAM setup |
| Restricted-data fitness | Weak (sharing links leak easily; check the VU data classification policy) | Good (per-folder ACLs, EU-hosted) | Good (private bucket, EU-hosted, SURF contract) | Good (project ACLs) | Only with a contract and EU region |
| Checksums (rclone) | MD5 | SHA1/MD5 (`owncloud` vendor) | MD5 (rclone stores it for multipart uploads) | md5sum over SSH (slow for TB) | MD5 |
| DVC remote | `gdrive` (needs your own OAuth client) | `webdav` | `s3` (native) | `ssh` | `s3` |
| Durability | Google redundancy | Versioning and trash bin (check the VU contract for backup) | Replicated Ceph. dCache can be tape-backed | **No backup**; deleted after the project ends | Provider SLA |
| Verdict | Documents, figures, small tables only | **Fallback** and easy exchange | **Recommended primary store** | Compute-side working copy | Only if SURF is unavailable, open products only |

dCache at SURF (WebDAV/xrootd with macaroons, tape-backed) suits grid-scale archival of large immutable files better than this project's analysis-ready parquet. Its onboarding is also heavier than the Object Store's, so the Object Store wins here.

## 3. Recommendation

1. **SURF Object Store (S3)** is the primary shared, versioned store: the DVC default remote `surf` and the `pyro-sync` remote `surf_s3`. It is fast, handles many-file parquet datasets, supports query-in-place, is EU-hosted, and is private with per-user keys. Step-1 volumes (≲ 0.5 TB) need only a small allocation.
2. **Snellius project space** is where raw model output stays and step 1b runs. Results move from Snellius to S3 with `pyro-sync` (Slurm `staging` partition).
3. **SURF Research Drive** is the fallback if the Object Store allocation is delayed. It is also the easiest way to give Jelle browsable access to restricted event-level tables.
4. **Google Drive** is for meeting material and figures only. The tool refuses restricted datasets there.

Actions for Jessie: request an Object Store allocation and keys through SURF, set up a Research Drive project folder, and export the variables from `config/remotes.env.example` in the shell or secret store. None of this goes into the repo.

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
scripts/sync.sh push events -r gdrive                            # refused: restricted dataset
```

From a laptop, Codespaces or VU ADA, the `snellius` remote (SFTP, ssh-agent key) pulls directly from project space: `scripts/sync.sh pull model_subsets -r snellius --apply`.

## 5. DVC for versioned snapshots

`pyro-sync` moves working directories. DVC pins **versions** of the datasets that analyses depend on, so a collaborator can check out a commit and run `dvc pull` to get exactly the matching data.

```bash
uv sync --extra dvc                       # or use the container
dvc add data/derived/model_subsets        # writes data/derived/model_subsets.dvc (committed)
git add data/derived/model_subsets.dvc data/derived/.gitignore && git commit -m "data: model subsets v1"
dvc push                                  # -> default remote `surf` (s3://pyro/dvc on objectstore.surf.nl)
# collaborator:
dvc pull data/derived/model_subsets.dvc
```

- Remotes are in `.dvc/config`: `surf` (default, S3), `researchdrive` (WebDAV) and `snellius` (SSH). Their URLs are placeholders; set the real ones with `dvc remote modify --local <name> url …`, which writes the gitignored `.dvc/config.local`. Credentials: `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` for `surf`, and `dvc remote modify --local researchdrive user/password` for WebDAV.
- **Restricted outputs** (collocation, aggregates, event tables) may only live in a private remote. Add `remote: surf` to their `.dvc` file, or use a separate private bucket. The `.dvc` pointer files hold only hashes and sizes, but their names are visible in this public repo, so keep event-specific names out of the paths.

## 6. Licensing and data-protection rules

- GLD360 lightning raw data (licensed) and unpublished fire-side event data **must never go to a public location**: not public buckets, not this public repo, not public DVC remotes. The sync tool enforces this with `restricted`/`never_sync`; reviewers should enforce it for everything else.
- The repo is public. Snellius paths, project and account names live only in `config/local.env`.
- Released products, once published, go to a `public = true` remote such as `public_release` or Zenodo, after a check by both sides.
