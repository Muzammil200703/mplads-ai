# Production deployment checklist

The frontend is a static Vite application on Vercel. The FastAPI backend must
be deployed separately and contain the real MPLADS database.

## Why this matters

`backend/mplads.db` is managed through Git LFS because it is larger than
ordinary Git permits. A normal Git checkout contains only a small LFS pointer,
not the dataset. Starting the API against that pointer creates a blank SQLite
schema, which can otherwise look like a valid dashboard with zero projects and
zero anomalies.

The repository now includes two safeguards:

- `backend/start.sh` refuses to start the production API unless the real
  dataset is present.
- `/health` returns a degraded response when no project records exist, and the
  dashboard withholds audit conclusions rather than displaying a misleading
  zero-risk result.

## Render backend

Use the included `render.yaml`, or configure the existing Render service with:

| Setting | Value |
| --- | --- |
| Build command | `git lfs pull && pip install -r backend/requirements.txt` |
| Start command | `cd backend && sh start.sh` |
| Health check | `/health` |
| Python | `3.12.8` |

After deployment, open `https://YOUR-RENDER-SERVICE/health`. It must return
`"data_ready": true` and a positive `total_projects` count before sharing the
demo link.

## Admin CSV imports (Data Management)

Admins upload official eSAKSHI CSVs from **Administration → Data Management**.
Uploads are staged under `backend/data/imports/` (gitignored) and applied in a
single SQLite transaction; a batch can be rolled back because each inserted
row id and each changed row's prior values are recorded in `import_changes`.

**Durability:** the Render free plan has an ephemeral filesystem, so an import
that writes to `backend/mplads.db` survives only until the next deploy or
restart. To keep imported data:

1. Attach a Render disk (e.g. mounted at `/var/data`).
2. Set `DATABASE_PATH=/var/data/mplads.db` (copy the Git-LFS database there on
   first boot).
3. Set `MPLADS_IMPORT_DIR=/var/data/imports` for staged uploads.

After an import, derived analytics refresh in the background (`project_rec_info`,
MP summary ledger fields, all risk scores, caches). Until that finishes the
batch shows `refreshing`; a failed refresh can be retried from the batch card
without re-importing rows.

## Vercel frontend

Set `VITE_API_URL` in the Vercel project environment to the exact Render
backend URL (without a trailing slash), then redeploy the frontend. Do not use
`http://127.0.0.1:8000` in a production Vercel build.
