"""Tests for AI editor integration, DB snapshot truncation safety, and card text routing."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "dummy_bot_token")
os.environ.setdefault("ADMIN_USER_ID", "123456")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "-100123456")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models import Base, Draft, DraftSnapshot, Project, ProjectStatus
from services.draft_content import ArtworkMetadata, DraftContent
from services.editor_service import EditPlan, EditorService, _fast_deterministic_parse
from services.openrouter_client import _extract_json_text
from services.version_manager import VersionManager


class TestAIEditorAndTruncationFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _sample_draft_content(self) -> DraftContent:
        return DraftContent(
            title="Flop Network: AI-Agent Testnet",
            category="TESTNET",
            description="Flop Labs is an advanced AI-agent network with testnet access.",
            tasks=[
                "Connect testnet wallet",
                "Mint test FLOP tokens",
                "Complete at least 3 agent transactions",
            ],
            potential_reward="$2500+",
            network="Base",
            project_link="https://flop.network",
            twitter_text="Flop Network testnet is live. Test agent transactions now: https://flop.network #airdrop",
            artwork=ArtworkMetadata(theme_color="lime"),
        )

    async def test_snapshot_long_action_truncation_safety(self):
        """Verify that long action strings (> 64 chars) do not crash save_snapshot."""
        content = self._sample_draft_content()
        long_action = 'Обновление фона карточки: измени Potential rewards на фото на "$1000+"'
        self.assertGreater(len(long_action), 64)

        async with self.session_factory() as session:
            project = Project(
                name="Flop Network",
                category="TESTNET",
                status=ProjectStatus.PENDING_REVIEW,
                dedup_hash="flop-12345",
                source="test",
            )
            session.add(project)
            await session.flush()

            draft = Draft(
                project_id=project.id,
                version=1,
                title=content.title,
                summary=content.description,
                instructions=content.render_instructions_text(),
                content_json=content.to_json(),
            )
            session.add(draft)
            await session.commit()

            snapshot = await VersionManager.save_snapshot(
                session,
                draft_id=draft.id,
                project_id=project.id,
                action=long_action,
                user_command='измени Potential rewards на фото на "$1000+"',
                edit_plan_json=None,
                content=content,
            )

            self.assertIsNotNone(snapshot.id)
            self.assertEqual(snapshot.version, 1)
            self.assertTrue(snapshot.action.startswith("Обновление фона карточки:"))

    async def test_exact_user_command_from_screenshot(self):
        """User command: 'измени Potential rewards на фото на "$1000+"'

        Must be routed to potential_reward (NOT image_background.regenerate),
        must rerender card text, and must update potential_reward to '$1000+'.
        """
        content = self._sample_draft_content()
        user_cmd = 'измени Potential rewards на фото на "$1000+"'

        # 1. Parsing
        plan = _fast_deterministic_parse(user_cmd, content)
        self.assertIsNotNone(plan, "Command must be parsed deterministically without LLM lag")
        self.assertEqual(plan.target, "potential_reward")
        self.assertEqual(plan.operation, "replace")
        self.assertEqual(plan.new_value, "$1000+")
        self.assertEqual(plan.image_operation, "rerender_text")
        self.assertFalse(plan.requires_confirmation)

        # 2. Application
        updated, is_valid, err = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)
        self.assertIsNone(err)
        self.assertEqual(updated.potential_reward, "$1000+")
        # Other content must stay intact
        self.assertEqual(updated.title, content.title)
        self.assertEqual(len(updated.tasks), 3)

    async def test_photo_text_variations_route_to_text(self):
        """Ensure various natural phrasings referencing text on photo/card route to text fields."""
        content = self._sample_draft_content()

        # Reward on photo
        p1 = _fast_deterministic_parse('поменяй награду на картинке на $500', content)
        self.assertIsNotNone(p1)
        self.assertEqual(p1.target, "potential_reward")
        self.assertEqual(p1.new_value, "$500")
        self.assertEqual(p1.image_operation, "rerender_text")

        # Title on photo
        p2 = _fast_deterministic_parse('смени заголовок на фото на: Base Season 2', content)
        self.assertIsNotNone(p2)
        self.assertEqual(p2.target, "project_title")
        self.assertEqual(p2.new_value, "Base Season 2")
        self.assertEqual(p2.image_operation, "rerender_text")

    async def test_color_theme_vs_art_generation(self):
        """Theme color commands route to local_edit; style/scene prompts route to new_artwork."""
        content = self._sample_draft_content()

        # Theme color edit (Pillow HUD recoloring)
        p_color = _fast_deterministic_parse('поменяй фон на синий', content)
        self.assertIsNotNone(p_color)
        self.assertEqual(p_color.target, "artwork")
        self.assertEqual(p_color.image_operation, "local_edit")
        self.assertEqual(p_color.new_value, "cyan")

        # Visual style generation (Flux AI)
        p_art = _fast_deterministic_parse('сделай фон в стиле киберпанк', content)
        self.assertIsNotNone(p_art)
        self.assertEqual(p_art.target, "image_background")
        self.assertEqual(p_art.image_operation, "new_artwork")

    def test_openrouter_json_extraction(self):
        """Test markdown codeblock stripping in openrouter client."""
        raw_1 = '{"title": "Test"}'
        self.assertEqual(_extract_json_text(raw_1), '{"title": "Test"}')

        raw_2 = '```json\n{"title": "Test"}\n```'
        self.assertEqual(_extract_json_text(raw_2), '{"title": "Test"}')

        raw_3 = 'Some text\n```\n{"title": "Test"}\n```'
        self.assertEqual(_extract_json_text(raw_3), '{"title": "Test"}')

    @patch("services.ai_editor_llm._call_llm_json")
    async def test_conversational_ai_edit_draft(self, mock_call):
        """Test conversational AI rework updates multiple draft fields intelligently."""
        from services.ai_editor_llm import ai_edit_draft

        content = self._sample_draft_content()
        mock_call.return_value = (
            {
                "title": "Flop Network ($30M Airdrop Confirmed)",
                "category": "TESTNET",
                "description": "Flop Network запустила открытый тестнет AI-агентов. Выполните 3 транзакции для квалификации в дропе.",
                "tasks": [
                    "Подключите кошелек в сети Base",
                    "Запросите тестовые токены FLOP",
                    "Совершите 3 свопа на DEX",
                ],
                "potential_reward": "$1500+",
                "network": "Base",
                "twitter_text": "Flop Network testnet is live with $30M allocated. Join now: https://flop.network #airdrop",
                "theme_color": "cyan",
                "image_operation": "rerender_text",
                "explanation": "Текст поста переписан лаконично на русском, награда обновлена на $1500+",
            },
            "OpenRouter (meta-llama/llama-3.3-70b-instruct:free)",
        )

        updated, meta = await ai_edit_draft(
            content,
            "Сделай описание лаконичным на русском, награду поставь $1500+ и тему синей",
        )

        self.assertEqual(updated.potential_reward, "$1500+")
        self.assertEqual(updated.artwork.theme_color, "cyan")
        self.assertEqual(len(updated.tasks), 3)
        self.assertTrue("Flop Network" in updated.title)
        self.assertEqual(meta["image_operation"], "rerender_text")
        self.assertIn("OpenRouter", meta["provider"])

    @patch("httpx.AsyncClient.post")
    async def test_openrouter_dual_model_payload(self, mock_post):
        """Verify OpenRouter client includes fallback model routing in request payload."""
        from services.openrouter_client import generate_chat_completion
        from config import settings

        mock_resp = unittest.mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": '{"status": "ok"}'}}],
            "model": "meta-llama/llama-3.3-70b-instruct:free",
        }
        mock_post.return_value = mock_resp

        with patch.object(settings, "OPENROUTER_API_KEY", "test_key"):
            res = await generate_chat_completion([{"role": "user", "content": "hello"}])
            self.assertEqual(res, '{"status": "ok"}')

            call_args = mock_post.call_args
            json_body = call_args.kwargs["json"]
            self.assertEqual(json_body["model"], "meta-llama/llama-3.3-70b-instruct:free")
            self.assertEqual(json_body["models"], [
                "meta-llama/llama-3.3-70b-instruct:free",
                "qwen/qwen-2.5-72b-instruct:free",
            ])
    @patch("services.ai_editor_llm._call_llm_json")
    async def test_ai_generate_initial_draft(self, mock_call):
        """Verify ai_generate_initial_draft parses OpenRouter response into DraftContent and DraftResult."""
        from services.ai_editor_llm import ai_generate_initial_draft

        mock_call.return_value = (
            {
                "title": "Lighter DEX: $30M Airdrop on Robinhood Chain",
                "category": "AIRDROP",
                "description": "Lighter has launched its perpetual DEX on Robinhood Chain with an 11M $LIT reward pool.",
                "tasks": [
                    "Connect wallet to Robinhood Chain",
                    "Execute testnet swaps on the DEX",
                    "Accumulate eligible trading volume",
                ],
                "potential_reward": "$1000+",
                "network": "Robinhood Chain",
                "twitter_text": "Lighter DEX is live on Robinhood Chain with $30M LIT airdrop pool. Trade now: https://lighter.xyz #airdrop",
                "theme_color": "violet",
                "image_prompt": "Futuristic neon purple perpetual DEX trading floor",
            },
            "OpenRouter (meta-llama/llama-3.3-70b-instruct:free)",
        )

        content, draft_res, prov = await ai_generate_initial_draft(
            name="Lighter",
            raw_text="Lighter is a perpetual DEX on Robinhood Chain with 11M LIT token airdrop pool.",
            chain="Robinhood Chain",
            category="AIRDROP",
            source_url="https://source.com/lighter",
            project_url="https://lighter.xyz",
        )

        self.assertEqual(content.title, "Lighter DEX: $30M Airdrop on Robinhood Chain")
        self.assertEqual(content.category, "AIRDROP")
        self.assertEqual(len(content.tasks), 3)
        self.assertEqual(content.potential_reward, "$1000+")
        self.assertEqual(content.artwork.theme_color, "violet")
        self.assertEqual(draft_res.twitter_text, "Lighter DEX is live on Robinhood Chain with $30M LIT airdrop pool. Trade now: https://lighter.xyz #airdrop")
        self.assertIn("OpenRouter", prov)

    async def test_twitter_edit_routing_and_application(self):
        """Verify deterministic parsing and application of Twitter edit commands."""
        content = self._sample_draft_content()
        new_tw = "Updated tweet with $30M reward! Join testnet: https://flop.network #airdrop"

        # 1. Parse
        plan = _fast_deterministic_parse(f"перепиши твит на: {new_tw}", content)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.target, "twitter")
        self.assertEqual(plan.new_value, new_tw)

        # 2. Apply
        updated, is_valid, err = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)
        self.assertIsNone(err)
        self.assertEqual(updated.twitter_text, new_tw)

    async def test_social_card_custom_artwork_resolution(self):
        """Verify that custom artwork path is properly resolved by render_social_card_from_content."""
        from services.social_card import render_social_card_from_content

        content = self._sample_draft_content()
        # Non-existent path returns None or falls back
        content.artwork.custom_artwork_path = "non_existent_artwork.png"
        res = await render_social_card_from_content(content)
        self.assertIsNotNone(res)


if __name__ == "__main__":
    unittest.main()
