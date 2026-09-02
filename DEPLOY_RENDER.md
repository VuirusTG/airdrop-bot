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
