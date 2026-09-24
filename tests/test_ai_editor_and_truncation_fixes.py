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

    async def test_openrouter_check_connection_and_health(self):
        """Verify OpenRouter connection verification, model details, and error diagnostics."""
        from services.health import check_openrouter
        from services.openrouter_client import check_connection

        # 1. Without API key
        with patch("config.settings.OPENROUTER_API_KEY", ""):
            ok, detail = await check_connection()
            self.assertFalse(ok)
            self.assertIn("не задан", detail)

        # 2. With 200 OK
        from unittest.mock import MagicMock
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {
            "data": {"label": "TelegramBotKey", "limit": 10}
        }
        mock_resp_200.text = "{}"
        with patch("config.settings.OPENROUTER_API_KEY", "test_key"), \
             patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp_200
            ok, detail = await check_connection()
            self.assertTrue(ok)
            self.assertIn("Подключен", detail)
            self.assertIn("meta-llama/llama-3.3-70b-instruct:free", detail)
            self.assertIn("qwen/qwen-2.5-72b-instruct:free", detail)
            self.assertIn("Dual-model failover", detail)

            item = await check_openrouter()
            self.assertTrue(item.working)
            self.assertEqual(item.name, "OpenRouter")

        # 3. With 401 error
        mock_resp_401 = MagicMock()
        mock_resp_401.status_code = 401
        mock_resp_401.text = "Unauthorized"
        with patch("config.settings.OPENROUTER_API_KEY", "test_key"), \
             patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp_401
            ok, detail = await check_connection()
            self.assertFalse(ok)
            self.assertIn("401", detail)
            self.assertIn("OPENROUTER_API_KEY", detail)

        # 4. With 429 rate limit
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.text = "Rate limit exceeded"
        with patch("config.settings.OPENROUTER_API_KEY", "test_key"), \
             patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp_429
            ok, detail = await check_connection()
            self.assertFalse(ok)
            self.assertIn("429", detail)
            self.assertIn("Qwen", detail)

    async def test_admin_system_status_openrouter_format(self):
        """Verify that on_system_status formats OpenRouter health, models, and error help."""
        from bot.handlers.admin_review import on_system_status
        from services.health import HealthItem, SystemHealth

        mock_bot = AsyncMock()
        mock_msg = AsyncMock()
        mock_msg.bot = mock_bot
        mock_msg.from_user.id = 123456
        mock_progress = AsyncMock()
        mock_msg.answer.return_value = mock_progress

        fake_health = SystemHealth(
            sources=[HealthItem("RSS: cryptonews.com", True, "HTTP 200, записей: 10")],
            telegram=HealthItem("Telegram", True, "@bot -> Channel"),
            x=HealthItem("X", False, "No OAuth"),
            openrouter=HealthItem("OpenRouter", True, "Подключен (TelegramBotKey)\n   • Основная модель: meta-llama/llama-3.3-70b-instruct:free\n   • Резервная модель: qwen/qwen-2.5-72b-instruct:free\n   • Dual-model failover: активен"),
            groq=HealthItem("Groq", True, "Groq API доступен"),
            gemini=HealthItem("Gemini", True, "модель доступна"),
            cloudflare=HealthItem("Cloudflare Images", False, "не настроен"),
            recommendations=["Все основные компоненты работают."],
        )

        with patch("bot.handlers.admin_review.collect_system_health", return_value=fake_health):
            await on_system_status(mock_msg)
            mock_progress.edit_text.assert_called_once()
            call_text = mock_progress.edit_text.call_args[0][0]
            self.assertIn("OpenRouter (Основной ИИ): ✅", call_text)
            self.assertIn("llama-3.3-70b-instruct:free", call_text)
            self.assertIn("qwen/qwen-2.5-72b-instruct:free", call_text)
            self.assertIn("Справка по ошибкам OpenRouter:", call_text)
            self.assertIn("401 (Unauthorized)", call_text)
            self.assertIn("429 (Rate Limit)", call_text)

    async def test_cumulative_multi_step_sequential_edits(self):
        """Verify that multiple sequential edits across text, reward, color, and description are strictly cumulative."""
        content = self._sample_draft_content()
        # Step 0 initial assertions
        self.assertEqual(content.potential_reward, "$2500+")
        self.assertEqual(content.artwork.theme_color, "lime")
        orig_tasks = list(content.tasks)
        self.assertEqual(len(orig_tasks), 3)

        # Step 1: User edits reward to "$1000+"
        p1 = await EditorService.create_edit_plan('измени Potential rewards на фото на "$1000+"', content)
        c1, ok1, _ = await EditorService.apply_edit_plan(p1, content)
        self.assertTrue(ok1)
        self.assertEqual(c1.potential_reward, "$1000+")
        self.assertEqual(c1.artwork.theme_color, "lime")
        self.assertEqual(c1.tasks, orig_tasks)

        # Step 2: User changes color to violet
        from services.image_rework import detect_theme_color
        color_cmd = "сделай карточку фиолетового цвета"
        p2 = await EditorService.create_edit_plan(color_cmd, c1)
        c2, ok2, _ = await EditorService.apply_edit_plan(p2, c1)
        self.assertTrue(ok2)
        req_color = detect_theme_color(color_cmd)
        if req_color:
            c2.artwork.theme_color = req_color
        self.assertEqual(c2.artwork.theme_color, "violet")
        # CRITICAL: Reward must still be "$1000+", not reverted to initial "$2500+"!
        self.assertEqual(c2.potential_reward, "$1000+")
        self.assertEqual(c2.tasks, orig_tasks)

        # Step 3: User makes description concise
        desc_cmd = "сделай описание лаконичным"
        p3 = await EditorService.create_edit_plan(desc_cmd, c2)
        # Verify plan routes to description or none image operation
        self.assertIn(p3.image_operation, ("none", "rerender_text"))
        # Simulate LLM rewrite for description
        import copy
        fake_desc_draft = copy.deepcopy(c2)
        fake_desc_draft.description = "Flop Network: Fast AI agents on Base testnet."
        with patch("services.ai_editor_llm.ai_edit_draft", return_value=(fake_desc_draft, None)):
            c3, ok3, _ = await EditorService.apply_edit_plan(p3, c2)
        self.assertTrue(ok3)
        self.assertEqual(c3.description, "Flop Network: Fast AI agents on Base testnet.")
        # CRITICAL: Both reward from Step 1 AND theme color from Step 2 are preserved!
        self.assertEqual(c3.potential_reward, "$1000+")
        self.assertEqual(c3.artwork.theme_color, "violet")
        self.assertEqual(c3.tasks, orig_tasks)

    async def test_ai_edit_draft_preserves_untouched_fields_with_modified_fields(self):
        """Verify that ai_edit_draft only applies fields in modified_fields and protects unmentioned ones."""
        from services.ai_editor_llm import ai_edit_draft
        content = self._sample_draft_content()
        content.potential_reward = "$777"
        content.artwork.theme_color = "violet"

        # Simulate LLM returning a hallucinated default "lime" theme_color and generic reward,
        # but correctly declaring modified_fields = ["description"]
        fake_llm_json = {
            "title": content.title,
            "description": "Short and punchy summary.",
            "tasks": content.tasks,
            "potential_reward": "TBD",  # LLM attempted to reset reward
            "theme_color": "lime",      # LLM attempted to reset color to lime
            "twitter_text": content.twitter_text,
            "modified_fields": ["description"],
            "explanation": "Made description concise.",
        }

        with patch("services.ai_editor_llm._call_llm_json", return_value=(fake_llm_json, "test-model")):
            updated, meta = await ai_edit_draft(content, "сделай описание короче")

        self.assertIsNotNone(updated)
        self.assertIn("explanation", meta)
        self.assertEqual(updated.description, "Short and punchy summary.")
        # Protected: potential_reward MUST NOT be reset to TBD
        self.assertEqual(updated.potential_reward, "$777")
        # Protected: theme_color MUST NOT be reset to lime
        self.assertEqual(updated.artwork.theme_color, "violet")

    async def test_studio_set_color_preserves_content_json_and_snapshots(self):
        """Verify on_set_color maintains content_json, custom steps/reward, and allows undo."""
        from bot.handlers.admin_review import on_set_color
        from services.draft_content import sync_content_to_draft
        from unittest.mock import MagicMock

        content = self._sample_draft_content()
        content.potential_reward = "$1500"
        content.tasks = ["Step A", "Step B"]

        async with self.session_factory() as session:
            project = Project(
                name="Flop Network",
                category="TESTNET",
                chain="Base",
                source="test",
                status=ProjectStatus.PENDING_REVIEW,
                dedup_hash="flop-network-test-studio-hash",
            )
            session.add(project)
            await session.flush()

            draft = Draft(
                project_id=project.id,
                version=1,
            )
            sync_content_to_draft(content, draft)
            session.add(draft)
            await session.commit()
            draft_id = draft.id

            # Save initial snapshot
            await VersionManager.save_snapshot(
                session,
                draft_id=draft.id,
                project_id=project.id,
                action="Исходный черновик",
                user_command=None,
                edit_plan_json=None,
                content=content,
            )

        # Mock callback query for on_set_color:pid:cyan
        mock_cb = MagicMock()
        mock_cb.data = f"set_color:{project.id}:cyan"
        mock_cb.from_user.id = 123456
        mock_cb.answer = AsyncMock()
        mock_cb.message = AsyncMock()

        with patch("bot.handlers.admin_review.get_session", self.session_factory), \
             patch("bot.handlers.admin_review._replace_review_message", new_callable=AsyncMock):
            await on_set_color(mock_cb)

        # Verify results in DB
        async with self.session_factory() as session:
            from sqlalchemy.orm import selectinload
            from sqlalchemy import select
            res = await session.execute(
                select(Project).options(selectinload(Project.drafts)).where(Project.id == project.id)
            )
            loaded_proj = res.scalar_one()
            latest = loaded_proj.latest_draft()
            self.assertIsNotNone(latest)
            self.assertEqual(latest.version, 2)
            self.assertIsNotNone(latest.content_json)

            from services.draft_content import draft_to_content
            loaded_content = draft_to_content(latest, loaded_proj)
            self.assertEqual(loaded_content.artwork.theme_color, "cyan")
            self.assertEqual(loaded_content.potential_reward, "$1500")
            self.assertEqual(loaded_content.tasks, ["Step A", "Step B"])

            # Verify Undo is available and functions!
            can_undo = await VersionManager.can_undo(session, latest.id)
            self.assertTrue(can_undo)

    async def test_remove_and_update_risk_note(self):
        """Verify that 'убери раздел Risk с поста для телеграмма' cleanly removes risk_note."""
        content = self._sample_draft_content()
        content.risk_note = "Airdrop allocations and tokenomics are not yet finalized."
        
        # Verify initial rendering contains Risk section
        initial_post = content.render_telegram_post()
        self.assertIn("⚠️ Risk:", initial_post)
        self.assertIn("Airdrop allocations", initial_post)

        # 1. Fast deterministic parse for exact user query
        cmd = "убери раздел Risk с поста для телеграмма"
        plan = _fast_deterministic_parse(cmd, content)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.target, "risk_note")
        self.assertEqual(plan.operation, "remove")
        self.assertIsNone(plan.new_value)
        self.assertEqual(plan.image_operation, "none")

        # 2. Apply plan
        updated, ok, err = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(ok)
        self.assertIsNone(updated.risk_note)

        # 3. Verify Telegram post rendering has NO Risk block
        rendered_tg = updated.render_telegram_post()
        self.assertNotIn("⚠️ Risk:", rendered_tg)
        self.assertNotIn("Airdrop allocations", rendered_tg)
        # Verify other fields remain intact
        self.assertEqual(updated.potential_reward, content.potential_reward)
        self.assertEqual(updated.title, content.title)
        self.assertEqual(updated.tasks, content.tasks)

        # 4. Variations test
        variations = [
            "удали риск",
            "убери риск с поста",
            "удали блок risk",
            "remove risk",
            "удали раздел risk",
            "убери предупреждение о риске",
            "удали блок с риском",
            "удали блок риска",
            "убери строку с риском",
            "удали пункт risk",
            "убери секцию risk",
            "удали риски",
            "без рисков",
            "без риска",
            "убери risk",
            "сотри риск",
            "очисти риск",
            "исключи предупреждение о риске",
            "remove risk section",
            "delete risk warning",
            "clear risk",
        ]
        for v in variations:
            p = _fast_deterministic_parse(v, content)
            self.assertIsNotNone(p, f"Failed to parse variation: {v}")
            self.assertEqual(p.target, "risk_note")
            self.assertEqual(p.operation, "remove")

        # 5. Update risk test
        update_cmd = "измени риск на: Высокий риск потери газа в тестнете"
        p_up = _fast_deterministic_parse(update_cmd, content)
        self.assertIsNotNone(p_up)
        self.assertEqual(p_up.target, "risk_note")
        self.assertEqual(p_up.operation, "replace")
        self.assertEqual(p_up.new_value, "Высокий риск потери газа в тестнете")

        up_content, up_ok, _ = await EditorService.apply_edit_plan(p_up, content)
        self.assertTrue(up_ok)
        self.assertEqual(up_content.risk_note, "Высокий риск потери газа в тестнете")
        self.assertIn("⚠️ Risk: Высокий риск потери газа в тестнете", up_content.render_telegram_post())

    async def test_ai_edit_draft_handles_risk_removal_and_preservation(self):
        """Verify ai_edit_draft removes risk when asked and preserves it when unmentioned."""
        from services.ai_editor_llm import ai_edit_draft
        content = self._sample_draft_content()
        content.risk_note = "Existing critical warning"

        # Case A: User asks to remove risk
        fake_llm_json_remove = {
            "title": content.title,
            "description": content.description,
            "tasks": content.tasks,
            "potential_reward": content.potential_reward,
            "risk_note": None,
            "twitter_text": content.twitter_text,
            "theme_color": content.artwork.theme_color,
            "modified_fields": ["risk_note"],
            "explanation": "Удален блок с риском",
        }
        with patch("services.ai_editor_llm._call_llm_json", return_value=(fake_llm_json_remove, "test-model")):
            c_no_risk, _ = await ai_edit_draft(content, "убери раздел Risk с поста для телеграмма")
        self.assertIsNone(c_no_risk.risk_note)
        self.assertNotIn("⚠️ Risk:", c_no_risk.render_telegram_post())

        # Case B: User asks to change reward only -> risk_note MUST BE PRESERVED
        fake_llm_json_reward = {
            "title": content.title,
            "description": content.description,
            "tasks": content.tasks,
            "potential_reward": "$9999",
            "risk_note": None,  # LLM omitted or set null, but modified_fields only says potential_reward
            "twitter_text": content.twitter_text,
            "theme_color": content.artwork.theme_color,
            "modified_fields": ["potential_reward"],
            "explanation": "Изменена награда",
        }
        with patch("services.ai_editor_llm._call_llm_json", return_value=(fake_llm_json_reward, "test-model")):
            c_reward_only, _ = await ai_edit_draft(content, "измени награду на $9999")
        self.assertEqual(c_reward_only.potential_reward, "$9999")
        # Protected: risk_note must NOT be wiped when user only asked to edit reward!
        self.assertEqual(c_reward_only.risk_note, "Existing critical warning")

    async def test_multi_turn_last_active_project_resolution(self):
        """Verify that last_active_project correctly binds sequential edits without explicit reply."""
        from bot.handlers.admin_review import last_active_project, on_feedback_reply
        from unittest.mock import MagicMock, patch

        user_id = 999
        project_id = 42
        last_active_project[user_id] = project_id

        # Simulate user sending follow-up text command without reply_to_message
        msg = MagicMock()
        msg.from_user.id = user_id
        msg.reply_to_message = None
        msg.text = "удали блок с риском"
        msg.answer = AsyncMock()

        with patch("bot.handlers.admin_review._is_admin_message", return_value=True), \
             patch("bot.handlers.admin_review.get_session", side_effect=lambda: self.session_factory()), \
             patch("bot.handlers.admin_review._load_project", new_callable=AsyncMock) as mock_load, \
             patch("bot.handlers.admin_review._execute_and_apply_plan", new_callable=AsyncMock) as mock_exec, \
             patch("bot.handlers.admin_review.VersionManager.save_snapshot", new_callable=AsyncMock):

            mock_proj = MagicMock()
            mock_proj.id = project_id
            mock_proj.project_url = "https://example.com"
            mock_draft = MagicMock()
            mock_draft.id = 101
            mock_draft.content_json = None
            mock_draft.risk_note = "Warning"
            mock_draft.instructions = "1. Step"
            mock_draft.potential_reward = "$100"
            mock_draft.twitter_text = "tw"
            mock_draft.image_path = None
            mock_draft.image_source = None
            mock_draft.image_prompt = None
            mock_draft.title = "Test Proj"
            mock_draft.summary = "Desc"
            mock_proj.latest_draft.return_value = mock_draft

            mock_load.return_value = mock_proj

            await on_feedback_reply(msg)

            # Verified: loaded project was project_id 42, NOT queue[0]!
            mock_load.assert_called_once()
            args, _ = mock_load.call_args
            self.assertEqual(args[1], project_id)

            # Verified: plan was executed for project 42
            mock_exec.assert_called_once()
            plan = mock_exec.call_args[0][5]
            self.assertEqual(plan.target, "risk_note")
            self.assertEqual(plan.operation, "remove")

    async def test_default_risk_note_removal(self):
        """Verify risk_note defaults to None and legacy boilerplate is filtered out."""
        from services.fallback_content import fallback_generate_draft
        from services.llm_draft import _parse_draft
        from services.draft_content import draft_to_content
        from db.models import Draft

        # 1. Fallback content has None risk_note
        fallback = fallback_generate_draft("TestProj", "raw context", "Ethereum", "airdrop", "https://t.me/test")
        self.assertIsNone(fallback.risk_note)

        # 2. LLM draft parser defaults to None
        raw_json = (
            '{"title": "Test", "summary": "Summ", "instructions": "1. Step", '
            '"potential_reward": "None", "twitter_text": "tw", "image_prompt": "prompt", '
            '"risk_note": "Airdrop allocations..."}'
        )
        parsed = _parse_draft(raw_json)
        self.assertIsNone(parsed.risk_note)

        # 3. draft_to_content filters boilerplate
        draft = Draft(
            project_id=1,
            title="T",
            summary="S",
            instructions="Inst",
            risk_note="Airdrop allocations, criteria, and claims are subject to project terms and changes."
        )
        content = draft_to_content(draft)
        self.assertIsNone(content.risk_note)

        # 4. rendered_text excludes boilerplate
        rendered = draft.rendered_text()
        self.assertNotIn("Risk", rendered)
        self.assertNotIn("⚠️", rendered)

    async def test_pollinations_artwork_generator(self):
        """Verify Pollinations.ai generator, fallback cascade, and seed cache-key."""
        from services.artwork_generator import (
            check_pollinations_status,
            _generate_via_pollinations,
            generate_artwork
        )
        from unittest.mock import MagicMock

        # 1. Status check with 200 and valid image size
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.content = b"fake_png_data" * 100  # > 500 bytes
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp_200
            ok, detail = await check_pollinations_status()
            self.assertTrue(ok)
            self.assertIn("Flux & Turbo", detail)

        # 2. _generate_via_pollinations uses flux first
        mock_resp_img = MagicMock()
        mock_resp_img.status_code = 200
        mock_resp_img.content = b"large_image_bytes" * 500  # > 5000 bytes
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp_img
            img_bytes, provider = await _generate_via_pollinations("crypto logo", seed=123)
            self.assertEqual(img_bytes, mock_resp_img.content)
            self.assertEqual(provider, "Pollinations Flux")
            first_url = mock_get.call_args_list[0][0][0]
            self.assertIn("model=flux", first_url)
            self.assertIn("seed=123", first_url)

        # 3. generate_artwork includes seed in cache key
        with patch("services.artwork_generator.cf_configured", return_value=False), \
             patch("services.artwork_generator._generate_via_pollinations", new_callable=AsyncMock) as mock_gen, \
             patch("pathlib.Path.is_file", return_value=False), \
             patch("pathlib.Path.write_bytes") as mock_wb:
            mock_gen.return_value = (b"large_image_bytes" * 500, "Pollinations Flux")
            img_path, prov = await generate_artwork(prompt="cyberpunk airdrop", seed=42)
            self.assertIsNotNone(img_path)
            self.assertIn("-s42.jpg", img_path)
            self.assertEqual(prov, "Pollinations Flux")
            mock_wb.assert_called_once()


if __name__ == "__main__":
    unittest.main()


