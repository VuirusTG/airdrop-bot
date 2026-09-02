# Airdrop/Testnet Alpha Bot

Free-first bot for finding crypto airdrop, testnet, retrodrop, and quest opportunities.

Current flow:

1. Scan free/limited sources on a schedule.
2. Deduplicate candidates.
3. Separate opportunity relevance from legitimacy/risk with Groq, then try backup Gemini or a conservative local fallback.
4. Discover a public project/action URL separately from the private research source.
5. Generate English Telegram and Twitter/X drafts plus an English image-generation prompt.
6. Render a free 1200x675 social card, using the source-page `og:image`/`twitter:image` as artwork when available.
7. Send the review card to your Telegram DM with Approve / Rework / Delete buttons.
8. On Approve, publish one Telegram photo post and attempt automatic X publication with the same image.
9. If X fails, report the reason and resend the image with an `Open in X` fallback button.

## Cost Model

| Piece | Cost | Notes |
|---|---:|---|
| Telegram bot + channel posting | Free | Telegram Bot API |
| Groq filter + draft generation | Free/limited | Primary cloud AI provider using an open-weight model |
| Gemini filter + draft generation | Free/limited | Retained as a backup cloud provider |
| AirdropAlert/RSS discovery | Free | Public RSS feeds |
| Trusted X discovery | Free but fragile | Uses public RSS mirrors such as Nitter/XCancel |
| Social card rendering | Free | Optional Cloudflare FLUX artwork plus a local Pillow layout |
| Twitter/X direct publishing | Pay per use | Official X API; manual `Open in X` fallback is free |
| Instagram | Optional | Disabled unless credentials are configured |

## Setup

```powershell
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill `.env`:

- `BOT_TOKEN`: create via `@BotFather`.
- `ADMIN_USER_ID`: your numeric Telegram user ID.
- `PUBLISH_CHANNEL_ID`: target Telegram channel, for example `@your_channel`.
- `GROQ_API_KEY`: primary Groq Console key for automatic scans and manual rework.
- `GEMINI_API_KEY`: optional Google AI Studio key retained as a backup when Groq fails.
- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`: optional OAuth 1.0a user credentials for automatic X posting.

## Run

```powershell
& ".\.venv\Scripts\python.exe" -m bot.main
```

Run the web dashboard in a second terminal:

```powershell
& ".\.venv\Scripts\python.exe" -m uvicorn web.app:app --reload
```

Then open `http://127.0.0.1:8000`. Set a long random `WEB_SECRET_KEY` before
saving provider credentials in the website settings.

The bot starts polling Telegram and launches the background source scanner.

Useful admin command:

```text
/scan_now
/status
/channel_status
```

This manually triggers a source scan without waiting for the interval.

`/scan_now` shows separate counts for filtered candidates, duplicates, fallback processing, and errors.
`/status` runs live checks for every configured source, Telegram publishing rights,
X OAuth, Groq, and backup Gemini, then returns recommendations. `/channel_status` is an alias.

Manual smoke test:

```powershell
& ".\.venv\Scripts\python.exe" seed_test_project.py
```

## Review Flow

Every accepted candidate arrives in your DM with:

- A private source URL shown only in the admin review card. It is never included in a published Telegram or X draft.
- A verified project/action URL included in both public drafts. Approval is blocked when no safe project URL can be found.
- A generated 16:9 social card with a consistent ninja mascot and verified-safe labels. Cloudflare generates only the project-themed background; the mascot is reused for brand recognition. The final image is reused by Telegram and X publishing.
- Text-only Rework keeps the existing image and does not spend a Cloudflare generation. Mention an image/background/color in feedback, or use `Regenerate image`, to explicitly create a new versioned card.
- `1. Черновик для телеграмм канала`: what will be published automatically.
- `2. Черновик для твиттера`: engaging English copy limited to 280 characters, with a factual hook, honest risk note, natural discussion prompt, and up to two hashtags.
- `3. Изображение`: source-page image when available and an AI image prompt.
- `Approve`: publishes the Telegram draft to your channel.
- `Rework`: reply with feedback and Groq regenerates both Telegram and Twitter/X versions; Gemini is tried only if Groq fails.
- `Delete`: archives the candidate so it is not offered again.

## Free Sources

Configured in `.env`:

- `ENABLE_AIRDROPALERT_SOURCE=true`
- `ENABLE_RSS_SOURCE=true`
- `ENABLE_TRUSTED_X_SOURCE=true`
- `ENABLE_FREE_X_FALLBACK=true`
- `SOURCE_SCAN_INTERVAL_MINUTES=60`
- `FILTER_MIN_SCORE=4.0`
- `FILTER_VERSION=2`
- `ENABLE_IMAGE_DISCOVERY=true`
- `ENABLE_SOCIAL_CARD_GENERATION=true`
- `SOCIAL_CARD_DIRECTORY=images/generated`
- `SOCIAL_CARD_MASCOT_PATH=images/brand/ninja-mascot.png`
- `CLOUDFLARE_API_TOKEN=`
- `CLOUDFLARE_ACCOUNT_ID=`
- `CLOUDFLARE_IMAGE_MODEL=@cf/black-forest-labs/flux-1-schnell`
- `CLOUDFLARE_IMAGE_STEPS=4`
- `ENABLE_PROJECT_LINK_DISCOVERY=true`
- `X_AUTO_PUBLISH=true`

The prefilter examines title/body text only, so account names and URLs cannot create false airdrop matches. Groq receives uncertain but relevant opportunities for human review and rejects generic news, trading calls, and critical scam patterns.

Trusted X is implemented through public RSS mirrors. This keeps it free, but mirrors can be rate-limited or unavailable. If this gets noisy or flaky, disable it with:

```env
ENABLE_TRUSTED_X_SOURCE=false
```

## Project Structure

```text
bot/                      aiogram bot and review handlers
db/                       SQLAlchemy models and SQLite setup
ingestion/                free source collectors and scheduler
services/                 Groq/Gemini logic, image discovery, and processing pipeline
publishing/               Telegram publishing plus manual Twitter/X marker
seed_test_project.py      local smoke-test signal
```

## Next Useful Improvements

- Add Telegram channel ingestion via Telethon for curated alpha channels.
- Add source health reporting to `/scan_now`.
- Add project-site discovery beyond source-page metadata.
- Add optional paid Twitter/X publisher behind a config flag later.
