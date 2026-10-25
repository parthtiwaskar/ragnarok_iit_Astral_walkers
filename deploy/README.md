# Deploying Ragnarisk

One container: the dashboard + API + SQLite database + the agents. It runs one trial at a time by default (`MAX_CONCURRENT_TRIALS`) and keeps its data in `/data`.

## 1. Environment keys

Copy `deploy/.env.example` to `deploy/.env` and fill it in. The ones that matter:

| Variable | Required | What it is |
|---|---|---|
| `APP_ACCESS_TOKEN` | **yes** | Password for the dashboard API. Generate: `python -c "import secrets;print(secrets.token_urlsafe(32))"`. The server refuses to start on a public address without it. |
| `DEEPSEEK_API_KEY` | one LLM key | Cheapest provider (https://platform.deepseek.com), OpenAI-compatible. |
| `ANTHROPIC_API_KEY` | | https://console.anthropic.com. Strongest results. |
| `OPENAI_API_KEY` | | OpenAI, or any OpenAI-compatible gateway such as OpenRouter (set `OPENAI_BASE_URL=https://openrouter.ai/api/v1`). |
| `GROQ_API_KEY` | | https://console.groq.com. Its free tier is too small to finish a full trial. |
| `NINE_REALMS_PROVIDER` | no | `deepseek`, `anthropic`, `openai` or `groq`. Empty = the first key set, in that order. |
| `DEEPSEEK_MODEL` / `ANTHROPIC_MODEL` / `NINE_REALMS_OPENAI_MODEL` / `GROQ_MODEL` | no | Defaults `deepseek-chat` / `claude-sonnet-5-5` / `gpt-4.1-mini` (`openai/gpt-4.1-mini` on OpenRouter) / `openai/gpt-oss-120b`. |

**Swapping keys:** auto-detection takes the first key that is *set*, whether or not it still works. After replacing a key, delete the
old one or set `NINE_REALMS_PROVIDER`. On start the server prints the provider and model it chose, and why; `/api/health` (with the
token) reports them too.
| `MAX_TRIALS_PER_DAY`, `TRIAL_COOLDOWN_SECONDS` | no | Spending caps (defaults 20 per UTC day, 30 s per client). |
| `MAX_LLM_CALLS`, `MAX_CASE_TOKENS` | no | Per-trial caps (defaults 150 calls, 2,500,000 tokens). |
| `MAX_CONCURRENT_TRIALS` | no | Trials running at once (default 1). Raise it for the final round if the LLM key has the rate headroom. |
| `LIVE_PACKS_DIR`, `WATCH_INTERVAL_SECONDS`, `MAX_UPLOAD_MB` | no | Live event-pack intake: watched drop folder (default `/data/live_packs`), poll seconds (0 = off), largest upload (default 50 MB). |
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

## 3b. Render (render.yaml)

`render.yaml` at the repository root is a Render Blueprint. It builds `deploy/Dockerfile`, mounts a 1 GB disk at `/data`, health-checks `/api/health`,
and generates `APP_ACCESS_TOKEN` for you.

1. Push the repository (it must contain `render.yaml`) to GitHub.
2. Render dashboard > **New > Blueprint** > connect GitHub > pick this repository > **Apply**.
3. When asked, paste `ANTHROPIC_API_KEY` (or leave it empty and add it later under **Environment**). Browsing works without it.
4. Wait for the build (a few minutes; the first start embeds the corpus once). Open the service URL.
5. The dashboard asks for an access token: copy `APP_ACCESS_TOKEN` from the service's **Environment** tab.

Plan and memory: the default is `starter` (512 MB) with `NINE_REALMS_EMBEDDER=hash` (about 35 MB of RAM; word search plus fuzzy matching).
For full semantic search (bge-small, about 1 GB RAM) switch the plan to `standard` (2 GB) and set `NINE_REALMS_EMBEDDER=auto`.
Persistent disks need a paid plan: on the free plan the data is lost on every restart and the service sleeps when idle.
Run a single instance: the database is SQLite and trials run one at a time per process.

Why the entrypoint: Render mounts the disk owned by root. The container starts as root only to fix that ownership, then drops to an
unprivileged user (uid 10001) before running the app. `docker compose` already starts unprivileged and skips this step.

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

## 7. Adding a new event pack while it runs (no restart, no code change)

Any of these ingests the pack, repairs what it can, quarantines what it cannot, and returns an intake report. The new documents are searchable at once.

```bash
# a) from the dashboard: Evidence page > Data intake > Upload pack (.zip), or drop the zip on the panel
# b) from a shell, with the access token
curl -X POST http://localhost:8765/api/ingest -H "Authorization: Bearer $APP_ACCESS_TOKEN" -H "X-Pack-Name: E9" --data-binary @pack_e9.zip
# c) drop the folder or zip into the watched directory (inside the volume), then optionally force a rescan
docker cp pack_e9 $(docker compose -f deploy/docker-compose.yml ps -q ragnarisk):/data/live_packs/
curl -X POST http://localhost:8765/api/ingest/scan -H "Authorization: Bearer $APP_ACCESS_TOKEN"
```

A pack is a folder with `manifest.json` and a `docs/` folder (zips with one wrapping folder are fine). Re-ingesting the same pack changes nothing; a pack with a changed
file is re-read. Unsafe archives (paths that escape the folder, symlinks, oversize) are refused.
Then ask the question in the dashboard. If it says "As of 25 January 2027", that date becomes the clock, with no field to fill in.
