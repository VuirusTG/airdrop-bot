# Render + Neon + GitHub Actions

Architecture:
- Render Free Web Service: FastAPI dashboard + Telegram webhook.
- Neon Free: PostgreSQL.
- GitHub Actions: one-shot scanner every 6 hours.

## Existing data
Before the first Render deployment, migrate `airdrop_bot.db` into Neon with a migration command. The migration preserves primary keys and copies projects, drafts, publications and web settings.

## Render
Deploy this repository as a Render Blueprint using `render.yaml`. Set:
- `WEBHOOK_BASE_URL` = your final `https://<service>.onrender.com` URL.
- `TELEGRAM_WEBHOOK_SECRET` = a long random string.
- `DATABASE_URL` = Neon PostgreSQL connection string.

The service listens on Render's `PORT`, exposes `/healthz`, and registers Telegram's webhook at `/telegram/webhook`.

## GitHub Actions
Add the same runtime/API secrets to GitHub repository Actions secrets. The scanner workflow runs at 00:00, 06:00, 12:00 and 18:00 UTC. To scan every 12 hours, change the cron to `0 */12 * * *`.

The scanner is one-shot: it connects to Neon, scans sources, processes candidates, sends Telegram review notifications, then exits. No background worker is needed on Render.

## Important
Render Free can spin down after inactivity. This architecture accepts that because the bot uses Telegram webhooks rather than polling, while the scanner is independent. Mutable state is stored in Neon, not on Render's filesystem.


## Final deployment checklist

1. Do not commit `.env`, `airdrop_bot.db`, `__pycache__`, or generated local files.
2. Render must have `DATABASE_URL` set to the Neon connection string. The app normalizes `postgresql://` to `postgresql+asyncpg://` and handles Neon `sslmode`/`channel_binding` parameters.
3. `WEBHOOK_BASE_URL` must be the exact public Render URL, without a trailing slash.
4. `TELEGRAM_WEBHOOK_SECRET` is a secret chosen by you; it is not obtained from Telegram.
5. GitHub Actions needs the runtime/API secrets separately because GitHub cannot read Render environment variables.
6. After deployment, open `/healthz`. A healthy response must show `status: ok` and `database: postgresql`.
7. The scheduled scanner runs independently in GitHub Actions; Render does not need an always-on worker.
