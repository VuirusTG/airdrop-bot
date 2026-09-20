"""Tests verifying the fixes for:
1. Full draft rewrite support ('перепиши пост', 'сделай нормальный текст черновика', etc.)
2. No robotic fallback text ('This draft was created without AI, so...')
3. No silent truncation of telegram post body in _review_caption
4. Groq & Gemini client compatibility without response_schema
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "dummy_bot_token")
os.environ.setdefault("ADMIN_USER_ID", "123456")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "-100123456")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.draft_content import DraftContent
from services.editor_service import EditorService, _fast_deterministic_parse, rewrite_draft_with_llm
from services.fallback_content import fallback_generate_draft
from bot.handlers.admin_review import _review_caption
from db.models import Project, Draft


class TestEditorReworkFixes(unittest.IsolatedAsyncioTestCase):

    def test_fallback_draft_no_robotic_disclaimer(self):
        """Verify fallback draft generation does not include robotic disclaimer."""
        res = fallback_generate_draft(
            name="Lighter Robinhood",
            raw_text="Lighter is a perpetual DEX live on Robinhood Chain with 11M LIT token rewards.",
            chain="Robinhood Chain",
            category="AIRDROP",
            project_url="https://lighter.xyz",
        )
        self.assertNotIn("This draft was created without AI", res.summary)
        self.assertNotIn("This draft was created without AI", res.instructions)
        # Should have clean actionable instructions
        self.assertTrue(len(res.instructions) > 10)

    def test_fast_parse_full_draft_rewrite(self):
        """Verify fast parser catches human rewrite requests without crashing into dummy tasks."""
        sample_draft = DraftContent(
            title="Lighter: Robinhood Airdrop",
            category="AIRDROP",
            description="Lighter DEX reward campaign.",
            tasks=["Open project portal.", "Trade on DEX.", "Monitor official channels."],
            potential_reward="$1000+",
            network="Robinhood Chain",
        )

        test_commands = [
            "перепиши пост",
            "переделай пост",
            "сделай нормальный текст черновика",
            "перепиши черновик",
            "улучши текст поста",
            "сделай шаги",
            "нормальный текст",
        ]

        for cmd in test_commands:
            plan = _fast_deterministic_parse(cmd, sample_draft)
            self.assertIsNotNone(plan, f"Failed to match command: {cmd}")
            self.assertEqual(plan.target, "full_draft")
            self.assertEqual(plan.operation, "rewrite")
            self.assertFalse(plan.requires_confirmation)
            self.assertEqual(plan.image_operation, "rerender_text")

    async def test_apply_edit_plan_full_draft_rewrite(self):
        """Verify apply_edit_plan executes full draft rewrite via rewrite_draft_with_llm."""
        from config import settings

        current = DraftContent(
            title="Lighter: Robinhood Airdrop",
            category="AIRDROP",
            description="Robotic text here.",
            tasks=["Task 1", "Task 2"],
            potential_reward="Unconfirmed",
            network="Robinhood Chain",
        )

        mock_llm_json = """{
            "title": "Lighter Perpetual DEX Airdrop",
            "category": "AIRDROP",
            "description": "Lighter is launching its orderbook perpetual DEX with a massive $30M reward pool.",
            "tasks": [
                "Bridge funds to Robinhood Chain",
                "Execute perpetual trades to generate onchain volume",
                "Maintain active trading status for leaderboard rewards"
            ],
            "potential_reward": "11M $LIT ($30M pool)",
            "network": "Robinhood Chain"
        }"""

        with patch.object(settings, "GROQ_API_KEY", "mock_key"):
            with patch("services.editor_service.generate_json", new=AsyncMock(return_value=mock_llm_json)):
                plan = await EditorService.create_edit_plan("перепиши пост нормально", current)
                updated, is_valid, err = await EditorService.apply_edit_plan(plan, current)

                self.assertTrue(is_valid)
                self.assertIsNone(err)
                self.assertEqual(updated.title, "Lighter Perpetual DEX Airdrop")
                self.assertIn("$30M reward pool", updated.description)
                self.assertEqual(len(updated.tasks), 3)
                self.assertEqual(updated.potential_reward, "11M $LIT ($30M pool)")
                self.assertEqual(updated.network, "Robinhood Chain")

    def test_review_caption_never_truncates_telegram_body(self):
        """Verify _review_caption preserves the full telegram body (including all tasks) even when caption is crowded."""
        project = Project(name="Lighter Robinhood", source_url="https://source.com/very/long/admin/url/for/internal/review/only")
        project.id = 42

        content = DraftContent(
            title="Lighter Perpetual DEX Airdrop",
            category="AIRDROP",
            description="Lighter is launching its high-performance perpetual orderbook DEX on Robinhood Chain.",
            tasks=[
                "Bridge USDC to Robinhood Chain using the official portal",
                "Open long or short positions to generate active trading volume",
                "Accumulate points on the season 1 leaderboard",
                "Join the official Discord for snapshot verification announcements",
            ],
            potential_reward="11M $LIT ($30M pool)",
            network="Robinhood Chain",
            project_link="https://lighter.xyz/airdrop",
        )

        draft = Draft(
            project_id=42,
            title=content.title,
            summary=content.description,
            instructions=content.render_instructions_text(),
            potential_reward=content.potential_reward,
            project_url=content.project_link,
            source_url="https://source.com/admin/very/long/url",
            twitter_text="Lighter Robinhood Airdrop is live! Complete tasks on Robinhood Chain to earn $LIT rewards. Check out all qualification criteria now! #airdrop #crypto",
            content_json=content.to_json(),
        )

        caption = _review_caption(project, draft, 1, 1)

        # Telegram photo caption limit is strictly 1024 characters
        self.assertLessEqual(len(caption), 1024)

        # All 4 tasks MUST be present in the caption! None should be cut off
        self.assertIn("1. Bridge USDC to Robinhood Chain using the official portal.", caption)
        self.assertIn("2. Open long or short positions to generate active trading volume.", caption)
        self.assertIn("3. Accumulate points on the season 1 leaderboard.", caption)
        self.assertIn("4. Join the official Discord for snapshot verification announcements.", caption)
        self.assertIn("11M $LIT ($30M pool)", caption)
        self.assertIn("Robinhood Chain", caption)


if __name__ == "__main__":
    unittest.main()
