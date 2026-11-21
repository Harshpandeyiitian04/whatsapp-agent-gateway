# Deploying (free / low-cost tiers)

These deployment notes and `render.yaml` are written but **were not deployed or tested** as part of this submission. Everything verified runs locally with `docker compose`.

## Option A: Render (Blueprint in `render.yaml`)

| Component | Render service | Free? | Notes (from Render docs, fetched 2026-11-20) |
|---|---|---|---|
| API (`/webhook`, `/metrics`) | Web service | Free plan exists | Free web services **spin down after 15 minutes idle**. The first webhook after idle waits for a cold start, which breaks the <200 ms ack target. Use a paid instance for a real webhook. |
| Mock lab | Web service | Free | Same spin-down caveat. Only needed for the demo. |
| Worker | Web service on the free plan | Free | Runs the same worker process and answers `/health`. Free web services sleep after 15 minutes without a visit. Open the worker URL once to wake it before a demo. |
| Postgres | Render Postgres | Free plan exists | **Free databases expire 30 days after creation**, have 1 GB of storage, and no backups. |

Steps:
1. Push the repo to GitHub. In Render, choose **New → Blueprint** and pick the repo.
2. Fill in the `sync: false` secrets: `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `ADMIN_TOKEN`, and optionally `LLM_API_KEY`. Set `LLM_API_KEY` on the worker only, as `render.yaml` does. `LLM_PROVIDER` defaults to `gemini`.
3. Migrations run when the API and the worker start. The free plan has no shell, and `python -m app.migrate` skips files that already ran.
4. Point the Meta app's webhook at `https://wa-gateway-api.onrender.com/webhook` and set the same verify token there.
5. Check that it's up: `curl https://wa-gateway-api.onrender.com/health`

Render's dashboard has the exact monthly prices for the plans. Its pricing page did not render its tables when fetched for this README, so no numbers are quoted here.

## Option B: Fly.io (cited prices, see COST.md)

- Run the API and the worker as two process groups from the same image, each on a `shared-cpu-1x` 1 GB machine ($6.70 per 30 days in iad/ewr).
- Mock lab: a `shared-cpu-1x` 256 MB machine ($2.19 per 30 days).
- Postgres: Fly Managed Postgres Basic ($38/month), or Neon's free plan (100 CU-hours and 1 GB storage per project) for a demo.

Example `fly.toml` process section:

```toml
[processes]
  api = "uvicorn app.api.main:app --host 0.0.0.0 --port 8080 --loop uvloop --http httptools"
  worker = "python -m app.worker.main"
```

## Production checklist (beyond this take-home)

- Set `ALLOW_RESET=false`. It is already the default outside compose.
- Use a long random `ADMIN_TOKEN`, and put the admin endpoints behind a private network or SSO.
- Set `WHATSAPP_MODE=graph` with a system-user access token. The Graph sender is a stub and has not been exercised.
- Use managed Postgres with point-in-time recovery, and alert on `queue_oldest_age_seconds`, `dlq_size`, `double_bookings` and the `circuit_opened` events.
