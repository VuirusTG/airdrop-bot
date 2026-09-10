# Final deployment fix

This build merges the Ninja Scout social-card generator with the review-queue fix and adds the scanner/webhook fixes requested after Render deployment.

## Fixed

1. `/scan_now` duplicate messages
   - Telegram webhook now acknowledges updates immediately instead of waiting for long scans.
   - Duplicate Telegram `update_id` deliveries are ignored for 15 minutes.
   - Manual scan runs in a background task and edits one progress message with the final result.
   - Concurrent scans are blocked by an async scanner lock.

2. Scanner returning `collected: 0`
   - RSS collection no longer requires a project-looking URL before the candidate reaches Groq/Gemini.
   - AirdropAlert and trusted X feeds are collected broadly and delegated to the AI filter.
   - RSS feeds accept action keywords/current-action markers and then rely on the AI filter.
   - Source-level candidate counts are logged.
   - Removed the BeautifulSoup locator warning caused by parsing plain strings as HTML.

3. Review queue / Previous / Next
   - `/review` opens one review card with Previous / Next / Approve / Rework / Delete / Regenerate buttons.
   - Candidate ingestion now sends one Telegram review message per candidate instead of a separate image message plus text message.
   - Previous/Next edits the same card whenever possible and safely replaces it if Telegram media/text types differ.
   - Approve/Delete automatically advance to the next pending draft.

4. Image regeneration
   - Regeneration updates the existing review card instead of sending a separate broken image message.
   - Cloudflare Workers AI remains the primary artwork generator.
   - Arbitrary project OG images are no longer used as the full fallback background.
   - If Cloudflare fails, a deterministic Ninja Scout cyberpunk fallback is generated from the bundled ninja mascot, keeping the same black/neon-green visual identity.
   - Reward, network, tasks, title and other readable text remain application-rendered rather than AI-rendered.

## Deployment

Replace the current repository contents with this build, commit and push to `main`. Render will redeploy automatically. The existing Neon PostgreSQL database and existing environment variables are preserved.
