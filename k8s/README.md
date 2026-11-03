# Kubernetes (optional)

**Most of the time you won't need this.** The heavy input (6.5 TB of 3-hourly PPE output) sits on Snellius, and Snellius jobs go through Slurm, not Kubernetes. For step 1, the cheapest path is Slurm plus Apptainer (`scripts/slurm/run_step.sbatch`), with the same image everywhere.

Kubernetes is worth it only if one of these becomes true:

- Satellite download and collocation move to a cloud or SURF Research Cloud cluster that sits next to the object store, and many independent per-event or per-month tasks have to run in parallel without a Slurm allocation.
- A collaborator without a Snellius account needs to rerun the satellite side.
- A service has to stay up all the time, for example a scheduled NRT event ingest.

Model subsetting (`pyro.steps.model_subset`) cannot run here, because its input never leaves Snellius. Only the steps that read from the object store are portable.

## Layout

| Manifest | Step | Status |
|---|---|---|
| `jobs/00-event-windows.yaml` | event table → per-event windows (`pyro-events windows`) | runnable now |
| `jobs/10-fetch-satellite.yaml` | PACE / VIIRS granules per event (Indexed Job) | planned module |
| `jobs/20-collocate.yaml` | model subset × satellite → collocation parquet (Indexed Job) | planned module |
| `jobs/90-sync.yaml` | push results to the object store (`pyro-sync`) | runnable now |

## Prerequisites (created out of band, never committed)

```bash
kubectl create namespace pyro
# Data-access and storage credentials, from your own secret store:
kubectl -n pyro create secret generic pyro-credentials \
  --from-literal=EARTHDATA_TOKEN="$EARTHDATA_TOKEN" \
  --from-literal=AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY"
# Non-secret rclone remote definition (see config/remotes.env.example):
kubectl -n pyro create configmap pyro-remotes --from-env-file=<(grep -v '^#' config/remotes.env.example | grep -v '^$')
# A ReadWriteMany volume named pyro-data (or switch the jobs to read/write S3 directly).
```

Restricted data, such as event tables and event-keyed products, can only go to a cluster whose storage is private to the project team. The same rule applies to the sync tool's `restricted_ok` remotes.
