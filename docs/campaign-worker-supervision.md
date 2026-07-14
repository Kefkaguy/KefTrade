# Campaign Worker Supervision

Milestone 5 supports Docker Compose supervision for the simulation-only campaign worker.

## Runtime

Use `deploy/campaign-worker.compose.yml` with the existing API image context:

```bash
docker compose -f deploy/campaign-worker.compose.yml up -d keftrade-campaign-worker
```

Required environment:

- `DATABASE_URL`
- `KEFTRADE_WORKER_ID`

Recommended worker identity:

```text
campaign-worker-${HOSTNAME}
```

Each worker identity must be unique. The worker registry and job leases expose duplicate identity symptoms through Mission Control and production-validation audits.

## Reliability Behavior

- Automatic startup: Compose service starts the Python worker module directly.
- Automatic restart: `restart: unless-stopped`.
- Graceful shutdown: send a normal container stop; Compose gives 30 seconds. A stop file path is also configured at `/tmp/keftrade-campaign-worker.stop`.
- Health check: verifies required worker environment is present.
- Structured logs: Docker `json-file` logging with rotation.
- Safety: worker only runs `app.workers.campaign_runner`; it does not expose live trading or broker routing.

## Validation

Production validation checks parse this Compose file and verify restart policy, health check, stop grace period, worker command, worker identity environment, and log rotation.
