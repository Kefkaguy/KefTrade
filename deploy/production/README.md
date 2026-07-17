# KefTrade OVH VPS Production Deployment

This deployment keeps the existing architecture intact:

```text
Vercel Next.js frontend
  -> HTTPS API
  -> OVH VPS Nginx
  -> FastAPI
  -> PostgreSQL
  -> research campaign worker
  -> persistent Docker volumes
```

No research logic, validation thresholds, or APIs are changed except deployment-required CORS configuration.

## Files

- `apps/api/Dockerfile` — production FastAPI/worker image.
- `deploy/production/docker-compose.prod.yml` — PostgreSQL, migration runner, API, worker, Nginx.
- `deploy/production/nginx/keftrade.conf` — reverse proxy with gzip, proxy headers, and websocket upgrade support.
- `deploy/production/.env.production.example` — required production environment variables.
- `deploy/production/bootstrap_ubuntu.sh` — Ubuntu Docker/firewall/fail2ban bootstrap.

## Required secrets

Create `/opt/keftrade/deploy/production/.env` from `.env.production.example`.

Required:

- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `CORS_ORIGINS`

Optional but expected for full production functionality:

- `OPENAI_API_KEY`
- `GROQ_API_KEY`
- `ALPACA_API_KEY`
- `ALPACA_API_SECRET`

## Server bootstrap

```bash
ssh ubuntu@15.204.114.198
sudo bash /opt/keftrade/deploy/production/bootstrap_ubuntu.sh
```

## Application deployment

```bash
sudo mkdir -p /opt/keftrade
sudo chown -R ubuntu:ubuntu /opt/keftrade
cd /opt/keftrade
git clone https://github.com/Kefkaguy/KefTrade.git .
git checkout codex/premium-research-workspace
cp deploy/production/.env.production.example deploy/production/.env
chmod 600 deploy/production/.env
```

Edit `deploy/production/.env` with real secrets and the final Vercel origin.

Then:

```bash
cd /opt/keftrade/deploy/production
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d postgres
docker compose -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.prod.yml up -d api worker nginx
docker compose -f docker-compose.prod.yml ps
```

## Validation commands

```bash
curl -fsS http://127.0.0.1/health
curl -fsS http://127.0.0.1/docs | head
docker compose -f /opt/keftrade/deploy/production/docker-compose.prod.yml ps
docker compose -f /opt/keftrade/deploy/production/docker-compose.prod.yml logs --tail=100 api
docker compose -f /opt/keftrade/deploy/production/docker-compose.prod.yml logs --tail=100 worker
docker compose -f /opt/keftrade/deploy/production/docker-compose.prod.yml exec postgres pg_isready -U keftrade -d keftrade
```

## Vercel frontend

Keep Next.js on Vercel. Set:

```text
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
```

Until DNS/TLS exists, direct IP HTTP can validate the backend but is not suitable for a production HTTPS Vercel site because browsers block mixed-content API calls from HTTPS frontend pages to HTTP APIs.

## TLS

Point `api.yourdomain.com` to `15.204.114.198`, then issue a certificate and update Nginx to listen on 443 with the certificate. Do not expose PostgreSQL publicly.
