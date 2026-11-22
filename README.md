# Flux — Cloudflare Python Worker (Phase 1)

This repository is being migrated from the original long-running `flux.py` application to Cloudflare's event-driven Python Workers runtime.

## Included in Phase 1

- Telegram webhook endpoint with a secret-token check.
- Owner-only bot handling when `TELEGRAM_OWNER_ID` is configured.
- `/start` and `/help` commands.
- AI text chat through the Anthropic Messages API.
- Persistent conversation history in Cloudflare D1.
- Live Startup Intelligence scans from Reddit, GitHub and Hacker News.
- `/startup`, `/startup_scan`, and `/startup_auto on|off` commands.
- 30-minute Cron Trigger; automatic scanning is OFF until enabled in Telegram.

## Not migrated yet

The original `flux.py` also contains Gmail and Google Calendar, flight search and price alerts, shopping/product search and alerts, voice transcription, image/file handling, WhatsApp, approval workflows and a Flask dashboard. Those features are **not included in this first Worker release**. They need separate Cloudflare-compatible adapters and testing; do not assume they work because this starter deploys.

## Cloudflare setup

1. Create a Cloudflare D1 database named `flux-db`.
2. Copy its database ID into `wrangler.toml` at `database_id`.
3. Set your Telegram numeric user ID in `TELEGRAM_OWNER_ID`.
4. Apply the migration: `npx wrangler d1 migrations apply flux-db --remote` (or use the D1 dashboard SQL console and run `migrations/0001_initial.sql`).
5. Add secrets (never commit them):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_WEBHOOK_SECRET` (random string, 32+ characters)
   - `ANTHROPIC_API_KEY`
6. Deploy using the Python Workers toolchain: `uv run pywrangler deploy`.
7. Set the Telegram webhook to `https://api.telegram.org/bot<TOKEN>/setWebhook` with JSON body containing `url: https://<worker-subdomain>.workers.dev/telegram/webhook` and `secret_token: <same webhook secret>`.
8. Test `https://<worker-subdomain>.workers.dev/health`, then message the bot with `/start`.

## Development notes

Cloudflare's Python entrypoint is `src/entry.py`, with `Default(WorkerEntrypoint).fetch()` and the `python_workers` compatibility flag. The scheduled handler uses `@handler` and `on_scheduled(event, env, ctx)`.

This is a migration foundation, not a claim that the original desktop/server Python app can run unchanged on Workers. Validate the build and test webhook delivery before adding more integrations.
