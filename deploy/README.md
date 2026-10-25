# Deploying Ragnarisk

One container: the dashboard + API + SQLite database + the agents. It runs one trial at a time and keeps its data in `/data`.

## 1. Environment keys

Copy `deploy/.env.example` to `deploy/.env` and fill it in. The ones that matter:

| Variable | Required | What it is |
|---|---|---|
| `APP_ACCESS_TOKEN` | **yes** | Password for the dashboard API. Generate: `python -c "import secrets;print(secrets.token_urlsafe(32))"`. The server refuses to start on a public address without it. |
| `ANTHROPIC_API_KEY` | one of these two | Needed to run new trials (https://console.anthropic.com). Recommended. |
| `GROQ_API_KEY` | | Alternative provider (https://console.groq.com). Its free tier is too small to finish a full trial. |
| `NINE_REALMS_PROVIDER` | no | `anthropic` or `groq`; empty picks whichever key is set. |
| `ANTHROPIC_MODEL` / `GROQ_MODEL` | no | Defaults `claude-sonnet-5-5` / `openai/gpt-oss-120b`. |
| `MAX_TRIALS_PER_DAY`, `TRIAL_COOLDOWN_SECONDS` | no | Spending caps (defaults 20 per UTC day, 30 s per client). |
| `MAX_LLM_CALLS`, `MAX_CASE_TOKENS` | no | Per-trial caps (defaults 150 calls, 1,000,000 tokens). |
| `ALLOWED_ORIGINS` | no | Extra browser origins allowed to POST (the same host always is). |
| `TRUST_PROXY` | no | `1` behind a reverse proxy, so limits use `X-Forwarded-For`. |
| `PORT` | no | Published port (default 8765). |

Everything else (`DATA_DIR`, `RUNEMARK_PACK`, `FASTEMBED_CACHE`, ...) is preset inside the image. Browsing saved cases and the corpus works with
no LLM key; the key is only used when someone starts a trial.

## 2. Run with Docker

```bash
cp deploy/.env.example deploy/.env      # then fill in APP_ACCESS_TOKEN and an LLM key
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
curl http://localhost:8765/api/health   # {"ok": true, "auth_required": true, ...}
```

`PACK_DIR` is the Runemark pack folder inside the repo (default `data/runemark`; set it in `deploy/.env` if yours differs).
The image bakes in the embedding model, runs as an unprivileged user with a read-only filesystem, and writes only to the `ragnarisk-data` volume.

## 3. Put TLS in front

The server speaks plain HTTP. Terminate HTTPS with a reverse proxy or your platform, forward `X-Forwarded-Proto` and `X-Forwarded-For`, and set `TRUST_PROXY=1`.
Example (Caddy): `ragnarisk.example.com { reverse_proxy localhost:8765 }`.

## 3b. Hosted platforms (Render, Railway, Fly, ...)

Deploy `deploy/Dockerfile` (build context = repository root), set the variables above in the platform's secret store, attach a persistent disk at `/data`,
and use `/api/health` as the health check. Run a single instance: the database is SQLite and trials run one at a time per process.

## 4. Operations

- **Data:** the database and agent memory live in the `/data` volume. Back it up: `docker run --rm -v ragnarisk_ragnarisk-data:/data -v $PWD:/b busybox tar czf /b/ragnarisk-data.tgz /data`.
- **Logs:** one line per request (`time client method path status ms`), no query strings, no tokens. `LOG_LEVEL=warning` silences them.
- **Rotating keys:** change the value in `deploy/.env` and run `docker compose ... up -d`. Rotate `APP_ACCESS_TOKEN` the same way.
- **Updating the corpus:** a changed pack is re-ingested on start (a fingerprint check); embeddings are rebuilt automatically.

## 5. What protects the deployment

- Every `/api` route except `/api/health` needs `Authorization: Bearer <APP_ACCESS_TOKEN>`; the dashboard asks for it and keeps it for the browser tab only.
- Starting trials is rationed (daily cap, per-client cooldown, one at a time) and cross-origin POSTs are refused.
- Strict security headers (CSP, frame denial, no sniffing, no referrer); internal errors are hidden from clients in production.
- API keys live only in the server environment and are never sent to the browser.

Limits to know: one instance, one trial at a time, plain HTTP inside the container, a single shared access token (no per-user accounts).
