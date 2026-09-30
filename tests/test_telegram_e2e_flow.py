import asyncio
import html
import os
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

# Configure test environment
os.environ.setdefault("BOT_TOKEN", "dummy_bot_token")
os.environ.setdefault("ADMIN_USER_ID", "123456")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "-100123456")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers.admin_review import last_active_project, on_feedback_reply, on_undo_edit
from db.database import _ensure_optional_columns
from db.models import Base, Draft, DraftSnapshot, Project, ProjectStatus
from services.draft_content import (
    ArtworkMetadata,
    DraftContent,
    draft_to_content,
    sync_content_to_draft,
)
from services.editor_service import EditorService
from services.social_card import render_social_card_from_content
from services.task_validator import validate_tasks
from services.version_manager import VersionManager


class TestTelegramE2EFlow(unittest.IsolatedAsyncioTestCase):
    """End-to-end integration tests for Telegram admin_review editing & undo flows.

    Verifies the full pipeline:
    Telegram admin message -> admin_review -> EditorService -> EditPlan -> DraftContent -> renderer -> Telegram/social card.
    """

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_optional_columns(conn)
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        @asynccontextmanager
        async def _test_get_session():
            async with self.Session() as s:
                yield s

        self.mock_get_session = _test_get_session
        self.admin_id = 123456
        last_active_project.clear()

    async def asyncTearDown(self):
        await self.engine.dispose()
        last_active_project.clear()

    def _create_mock_message(self, text: str, user_id: int = 123456) -> MagicMock:
        msg = MagicMock()
        msg.from_user.id = user_id
        msg.chat.id = 123456
        msg.message_id = 777
        msg.text = text
        msg.reply_to_message = None

        sent_mock = MagicMock()
        sent_mock.chat.id = 123456
        sent_mock.message_id = 888

        msg.answer = AsyncMock(return_value=sent_mock)
        msg.answer_photo = AsyncMock(return_value=sent_mock)
        msg.reply = AsyncMock(return_value=sent_mock)
        msg.bot = MagicMock()
        msg.bot.delete_message = AsyncMock()
        return msg

    def _create_mock_callback(self, project_id: int, user_id: int = 123456) -> MagicMock:
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.data = f"undo_edit:{project_id}"
        cb.answer = AsyncMock()

        cb_msg = MagicMock()
        cb_msg.chat.id = 123456
        cb_msg.message_id = 888
        cb_msg.photo = [MagicMock()]
        cb_msg.edit_media = AsyncMock()
        cb_msg.edit_text = AsyncMock()
        cb_msg.delete = AsyncMock()

        cb.message = cb_msg
        cb.bot = MagicMock()
        cb.bot.send_photo = AsyncMock(return_value=cb_msg)
        cb.bot.send_message = AsyncMock(return_value=cb_msg)
        return cb

    async def _setup_project_and_draft(self) -> tuple[int, int]:
        """Creates initial project and draft in DB with baseline snapshot."""
        initial_content = DraftContent(
            title="Chips Network",
            category="AIRDROP",
            description="Decentralized points exchange protocol on Arbitrum.",
            tasks=[
                "Connect your MetaMask wallet to the official portal to qualify for airdrop",
                "Bridge at least 0.05 ETH to Arbitrum One using the official bridge portal to earn maximum points",
                "Complete the onboarding quest on Layer3 in order to receive your early supporter badge",
            ],
            potential_reward="$2500+",
            network="Arbitrum",
            project_link="https://chips.network",
            source_url="https://x.com/chips_network",
        )

        # Initial social card render
        initial_card = await render_social_card_from_content(initial_content)
        if initial_card:
            initial_content.artwork.path = initial_card.path
            initial_content.artwork.source = initial_card.source

        async with self.Session() as session:
            project = Project(
                dedup_hash="test-hash-e2e-chips",
                name="Chips Network",
                chain="Arbitrum",
                category="AIRDROP",
                source="test",
                source_url="https://x.com/chips_network",
                project_url="https://chips.network",
                status=ProjectStatus.PENDING_REVIEW,
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)

            draft = Draft(
                project_id=project.id,
                version=1,
                title=initial_content.title,
                summary=initial_content.description,
                instructions=initial_content.render_instructions_text(),
                potential_reward=initial_content.potential_reward,
                content_json=initial_content.to_json(),
                image_path=initial_content.artwork.path,
                image_source=initial_content.artwork.source,
                project_url=initial_content.project_link,
                source_url=initial_content.source_url,
            )
            session.add(draft)
            await session.commit()
            await session.refresh(draft)

            # Baseline snapshot v1
            await VersionManager.save_snapshot(
                session,
                draft_id=draft.id,
                project_id=project.id,
                action="Исходный черновик",
                user_command=None,
                edit_plan_json=None,
                content=initial_content,
            )
            return project.id, draft.id

    # -------------------------------------------------------------------------
    # Scenario 1: "Измени Potential Rewards с $2500+ на $1000+"
    # -------------------------------------------------------------------------
    async def test_scenario_1_reward_edit_flow(self):
        """Scenario 1:
        Command: 'Измени Potential Rewards с $2500+ на $1000+'
        Verify:
        - target == potential_reward
        - changes only reward
        - tasks, title, network remain unchanged
        - generate_artwork is NOT called
        - Telegram preview contains $1000+
        - Social card contains $1000+
        """
        project_id, draft_id = await self._setup_project_and_draft()
        last_active_project[self.admin_id] = project_id

        msg = self._create_mock_message("Измени Potential Rewards с $2500+ на $1000+")

        with patch("bot.handlers.admin_review._is_admin_message", return_value=True), \
             patch("bot.handlers.admin_review.get_session", side_effect=self.mock_get_session), \
             patch("bot.handlers.admin_review.generate_artwork", new_callable=AsyncMock) as mock_generate_artwork:

            await on_feedback_reply(msg)

            # 1. generate_artwork must NOT be called
            mock_generate_artwork.assert_not_called()

            # 2. Check Telegram preview message sent
            msg.answer_photo.assert_called()
            sent_caption = msg.answer_photo.call_args[1].get("caption", "")
            self.assertIn("$1000+", sent_caption)
            self.assertNotIn("$2500+", sent_caption)

            # 3. Verify DB state and content
            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                self.assertIsNotNone(draft)
                self.assertEqual(draft.potential_reward, "$1000+")
                self.assertEqual(draft.version, 2)

                content = draft_to_content(draft)
                self.assertEqual(content.potential_reward, "$1000+")
                self.assertEqual(content.title, "Chips Network")
                self.assertEqual(content.network, "Arbitrum")
                self.assertEqual(len(content.tasks), 3)

                # Telegram post text contains $1000+
                tg_post = content.render_telegram_post()
                self.assertIn("$1000+", tg_post)
                self.assertNotIn("$2500+", tg_post)

                # Social card file exists and reflects updated content
                self.assertTrue(os.path.exists(content.artwork.path))
                self.assertGreater(os.path.getsize(content.artwork.path), 2000)

    # -------------------------------------------------------------------------
    # Scenario 2: "Сделай список задач короче"
    # -------------------------------------------------------------------------
    async def test_scenario_2_shorten_tasks_flow(self):
        """Scenario 2:
        Command: 'Сделай список задач короче'
        Verify:
        - target == tasks
        - changes only tasks
        - every task passes validator
        - no '...' and '…'
        - no fictitious tasks
        - TG post and social card receive the exact same new list
        """
        project_id, draft_id = await self._setup_project_and_draft()
        last_active_project[self.admin_id] = project_id

        msg = self._create_mock_message("Сделай список задач короче")

        with patch("bot.handlers.admin_review._is_admin_message", return_value=True), \
             patch("bot.handlers.admin_review.get_session", side_effect=self.mock_get_session), \
             patch("bot.handlers.admin_review.generate_artwork", new_callable=AsyncMock) as mock_generate_artwork:

            await on_feedback_reply(msg)

            # generate_artwork must NOT be called for task shortening
            mock_generate_artwork.assert_not_called()

            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                self.assertIsNotNone(draft)
                content = draft_to_content(draft)

                # 1. Other fields remain strictly untouched
                self.assertEqual(content.title, "Chips Network")
                self.assertEqual(content.description, "Decentralized points exchange protocol on Arbitrum.")
                self.assertEqual(content.potential_reward, "$2500+")
                self.assertEqual(content.network, "Arbitrum")

                # 2. Every task passes validator
                v = validate_tasks(content.tasks)
                self.assertTrue(v.is_valid, f"Validation failed: {v.error_summary}")

                # 3. No ellipsis in any task
                for t in content.tasks:
                    self.assertNotIn("...", t)
                    self.assertNotIn("…", t)
                    self.assertLessEqual(len(t), 120)

                # 4. No fictitious tasks: preserved exact factual actions in concise format
                self.assertEqual(len(content.tasks), 3)
                self.assertTrue(any("Connect" in t for t in content.tasks))
                self.assertTrue(any("Bridge" in t for t in content.tasks))
                self.assertTrue(any("Layer3" in t or "quest" in t.lower() for t in content.tasks))

                # Tasks are shorter than originals
                self.assertLess(len(content.tasks[0]), len("Connect your MetaMask wallet to the official portal to qualify for airdrop"))
                self.assertLess(len(content.tasks[1]), len("Bridge at least 0.05 ETH to Arbitrum One using the official bridge portal to earn maximum points"))

                # 5. TG post and social card receive the exact same new list
                tg_post = content.render_telegram_post()
                for i, t in enumerate(content.tasks, 1):
                    self.assertIn(f"{i}. {t}.", tg_post)

                # Preview caption contains all shortened tasks
                msg.answer_photo.assert_called()
                caption = msg.answer_photo.call_args[1].get("caption", "")
                for t in content.tasks:
                    self.assertIn(t, caption)

                # Social card exists and was rendered with the same content
                self.assertTrue(os.path.exists(content.artwork.path))

    # -------------------------------------------------------------------------
    # Scenario 3: "Замени второй пункт на: Claim all available Chips points."
    # -------------------------------------------------------------------------
    async def test_scenario_3_replace_second_task_flow(self):
        """Scenario 3:
        Command: 'Замени второй пункт на: Claim all available Chips points.'
        Verify:
        - strictly modifies tasks[1]
        - tasks[0] and tasks[2] remain unchanged
        - card and TG text are synchronous
        """
        project_id, draft_id = await self._setup_project_and_draft()
        last_active_project[self.admin_id] = project_id

        # Read initial tasks
        async with self.Session() as session:
            initial_draft = await session.get(Draft, draft_id)
            init_content = draft_to_content(initial_draft)
            task_0_before = init_content.tasks[0]
            task_2_before = init_content.tasks[2]

        msg = self._create_mock_message("Замени второй пункт на: Claim all available Chips points.")

        with patch("bot.handlers.admin_review._is_admin_message", return_value=True), \
             patch("bot.handlers.admin_review.get_session", side_effect=self.mock_get_session), \
             patch("bot.handlers.admin_review.generate_artwork", new_callable=AsyncMock) as mock_generate_artwork:

            await on_feedback_reply(msg)

            mock_generate_artwork.assert_not_called()

            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                content = draft_to_content(draft)

                # 1. tasks[1] strictly modified
                self.assertEqual(content.tasks[1], "Claim all available Chips points")

                # 2. tasks[0] and tasks[2] remain untouched
                self.assertEqual(content.tasks[0], task_0_before)
                self.assertEqual(content.tasks[2], task_2_before)

                # 3. Card and TG text are synchronous
                tg_post = content.render_telegram_post()
                self.assertIn("2. Claim all available Chips points.", tg_post)
                self.assertIn(task_0_before, tg_post)
                self.assertIn(task_2_before, tg_post)

                msg.answer_photo.assert_called()
                caption = msg.answer_photo.call_args[1].get("caption", "")
                self.assertIn("Claim all available Chips points", caption)

    # -------------------------------------------------------------------------
    # Scenario 4: "Полностью создай новый фон в cyberpunk стиле"
    # -------------------------------------------------------------------------
    async def test_scenario_4_cyberpunk_artwork_flow(self):
        """Scenario 4:
        Command: 'Полностью создай новый фон в cyberpunk стиле'
        Verify:
        - target == image_background / card_background
        - generate_artwork IS called
        - title, tasks, reward, network remain unchanged
        """
        project_id, draft_id = await self._setup_project_and_draft()
        last_active_project[self.admin_id] = project_id

        # Read initial state
        async with self.Session() as session:
            initial_draft = await session.get(Draft, draft_id)
            init_content = draft_to_content(initial_draft)
            title_before = init_content.title
            tasks_before = list(init_content.tasks)
            reward_before = init_content.potential_reward
            network_before = init_content.network

        msg = self._create_mock_message("Полностью создай новый фон в cyberpunk стиле")

        with patch("bot.handlers.admin_review._is_admin_message", return_value=True), \
             patch("bot.handlers.admin_review.get_session", side_effect=self.mock_get_session), \
             patch("bot.handlers.admin_review.generate_artwork", new_callable=AsyncMock) as mock_generate_artwork:

            mock_generate_artwork.return_value = ("images/generated/cyberpunk_art.jpg", "pollinations")

            await on_feedback_reply(msg)

            # 1. generate_artwork was called with cyberpunk prompt
            mock_generate_artwork.assert_called_once()
            call_kwargs = mock_generate_artwork.call_args[1]
            self.assertIn("cyberpunk", call_kwargs.get("prompt", "").lower())

            # 2. Text fields are strictly preserved
            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                content = draft_to_content(draft)

                self.assertEqual(content.title, title_before)
                self.assertEqual(content.tasks, tasks_before)
                self.assertEqual(content.potential_reward, reward_before)
                self.assertEqual(content.network, network_before)

    # -------------------------------------------------------------------------
    # Scenario 5: Two consecutive edits + double Undo
    # -------------------------------------------------------------------------
    async def test_scenario_5_consecutive_edits_and_double_undo(self):
        """Scenario 5:
        - Edit 1: change reward to $500+
        - Edit 2: replace task 1 with 'Stake CHIPS tokens on official portal'
        - Undo 1 -> reverts to state after Edit 1 ($500+ reward, original task 1)
        - Undo 2 -> reverts to baseline state ($2500+ reward, original tasks)
        """
        project_id, draft_id = await self._setup_project_and_draft()
        last_active_project[self.admin_id] = project_id

        with patch("bot.handlers.admin_review._is_admin_message", return_value=True), \
             patch("bot.handlers.admin_review._is_admin_callback", return_value=True), \
             patch("bot.handlers.admin_review.get_session", side_effect=self.mock_get_session), \
             patch("bot.handlers.admin_review.generate_artwork", new_callable=AsyncMock):

            # --- EDIT 1: Change reward to $500+ ---
            msg1 = self._create_mock_message("Измени Potential Rewards на $500+")
            await on_feedback_reply(msg1)

            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                self.assertEqual(draft.version, 2)
                self.assertEqual(draft.potential_reward, "$500+")

            # --- EDIT 2: Replace task 1 ---
            msg2 = self._create_mock_message("Замени первый пункт на: Stake CHIPS tokens on official portal")
            await on_feedback_reply(msg2)

            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                self.assertEqual(draft.version, 3)
                self.assertEqual(draft.potential_reward, "$500+")
                content_v3 = draft_to_content(draft)
                self.assertEqual(content_v3.tasks[0], "Stake CHIPS tokens on official portal")

            # --- UNDO 1: Revert Edit 2 (task change) ---
            cb1 = self._create_mock_callback(project_id)
            await on_undo_edit(cb1)

            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                self.assertEqual(draft.version, 2)
                content_after_undo1 = draft_to_content(draft)
                # Task 1 reverted to original
                self.assertTrue(content_after_undo1.tasks[0].startswith("Connect your MetaMask"))
                # Reward is still $500+ from Edit 1
                self.assertEqual(content_after_undo1.potential_reward, "$500+")
                self.assertEqual(draft.potential_reward, "$500+")

                can_undo_again = await VersionManager.can_undo(session, draft.id)
                self.assertTrue(can_undo_again)

            # --- UNDO 2: Revert Edit 1 back to baseline ---
            cb2 = self._create_mock_callback(project_id)
            await on_undo_edit(cb2)

            async with self.Session() as session:
                draft = await session.get(Draft, draft_id)
                self.assertEqual(draft.version, 1)
                content_baseline = draft_to_content(draft)
                # Baseline reward restored!
                self.assertEqual(content_baseline.potential_reward, "$2500+")
                self.assertEqual(draft.potential_reward, "$2500+")
                # Baseline task restored!
                self.assertTrue(content_baseline.tasks[0].startswith("Connect your MetaMask"))

                # Cannot undo further
                can_undo_third = await VersionManager.can_undo(session, draft.id)
                self.assertFalse(can_undo_third)


if __name__ == "__main__":
    unittest.main()
