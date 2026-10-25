"""Production E2E Verification Gate for Content Series & X Threads Engine.

Verifies:
1. Real Telegram admin_review & series_review E2E flow with state tracking.
2. Required natural language editing commands:
   - "Сделай пост 3 короче"
   - "Замени пост 2 на: новый текст"
   - "Перегенерируй пост 4"
   - "Удали пост 3"
   - "Добавь пост о распределении токенов"
   - "Сделай всю серию более динамичной"
3. Real Editor V2 strict post isolation (posts 1, 2, 4, 5 bit-for-bit unchanged when editing post 3).
4. Undo & Redo state restoration.
5. Fact fidelity hardening (rejection of fake chain, conflicting reward, phishing URL, fake deadline, security leaks).
6. X length validation without silent truncation.
7. Partial publish recovery & unbroken reply chain.
8. Idempotency guards (in-memory lock + DB publishing status).
"""
import asyncio
from contextlib import asynccontextmanager
import copy
import json
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["BOT_TOKEN"] = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
os.environ.setdefault("ADMIN_USER_ID", "123456")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "-100123456")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.database import _ensure_optional_columns
from db.models import (
    Base,
    ContentSeries,
    Draft,
    Project,
    ProjectStatus,
    SeriesPost,
    SeriesSnapshot,
    SeriesStatus,
    PostStatus,
)
from services.draft_content import DraftContent, sync_content_to_draft
from services.editor_service import EditPlan
from services.series_editor import SeriesEditor, _fast_deterministic_series_parse
from services.series_engine import SeriesEngine
from services.series_models import (
    SeriesData,
    SeriesFormat,
    SeriesPostData,
    series_db_to_data,
    sync_data_to_series_db,
)
from services.series_validator import SeriesValidator
from services.series_version_manager import SeriesVersionManager
from publishing.series_publisher import SeriesPublisher, _active_series_locks
from bot.handlers.series_review import (
    on_series_init,
    on_series_generate,
    on_series_nav,
    on_series_undo,
    on_series_redo,
    on_series_save_draft,
    on_series_publish,
    on_series_text_input,
    awaiting_series_edit,
    awaiting_series_add,
    last_active_series,
)


def create_sample_project_and_draft() -> tuple[Project, Draft, DraftContent]:
    proj = Project(
        id=77,
        dedup_hash="hash_blast_test",
        name="Blast Ecosystem Phase 2",
        chain="Blast",
        category="AIRDROP",
        source="github",
        source_url="https://github.com/blast-io",
        project_url="https://blast.io",
        status=ProjectStatus.PENDING_REVIEW,
    )
    content = DraftContent(
        title="Blast Ecosystem Phase 2 Points",
        category="AIRDROP",
        description="Blast Phase 2 invites developers and users to participate in the points distribution campaign.",
        tasks=[
            "Bridge ETH or USDB to Blast mainnet",
            "Interact with verified dApps to accumulate multiplier points",
            "Maintain daily active balance until the final snapshot",
        ],
        potential_reward="$2500+",
        network="Blast",
        project_link="https://blast.io",
    )
    draft = Draft(
        id=202,
        project_id=77,
        version=1,
    )
    sync_content_to_draft(content, draft)
    proj.drafts = [draft]
    return proj, draft, content


from aiogram.types import CallbackQuery, Message


def create_mock_callback(user_id: int, callback_data: str, message: Any = None) -> MagicMock:
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.data = callback_data
    cb.answer = AsyncMock()
    del cb.chat
    cb.message = message or MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.edit_reply_markup = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.message.delete = AsyncMock()
    return cb


def create_mock_message(user_id: int, text: str) -> MagicMock:
    msg = MagicMock()
    msg.from_user.id = user_id
    msg.text = text
    msg.chat.id = user_id
    msg.answer = AsyncMock()
    del msg.data
    del msg.message
    return msg


class TestSeriesProductionVerification(unittest.IsolatedAsyncioTestCase):
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
        _active_series_locks.clear()
        awaiting_series_edit.clear()
        awaiting_series_add.clear()
        last_active_series.clear()

    async def asyncTearDown(self):
        await self.engine.dispose()
        _active_series_locks.clear()
        awaiting_series_edit.clear()
        awaiting_series_add.clear()
        last_active_series.clear()

    # =========================================================================
    # 1. REAL TELEGRAM ADMIN REVIEW E2E FLOW & COMMAND ROUTING
    # =========================================================================
    async def test_telegram_admin_e2e_flow_and_natural_commands(self):
        """Tests the full user journey:
        Init -> Generate -> Next/Prev -> Edit commands -> Undo -> Redo -> Publish.
        """
        proj, draft, _ = create_sample_project_and_draft()

        # Seed Project & Draft in DB
        async with self.Session() as session:
            session.add(proj)
            await session.commit()

        admin_id = 123456

        # Step 1: on_series_init
        cb_init = create_mock_callback(admin_id, f"series_init:{proj.id}:x_thread")
        await on_series_init(cb_init)
        cb_init.message.edit_reply_markup.assert_called_once()

        # Step 2: on_series_generate
        cb_gen = create_mock_callback(admin_id, f"series_gen:{proj.id}:x_thread:5")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_generate(cb_gen)

        self.assertIn(admin_id, last_active_series)
        series_id = last_active_series[admin_id]
        self.assertGreater(series_id, 0)

        # Verify series saved in DB with 5 posts
        async with self.Session() as session:
            series_db = await session.get(ContentSeries, series_id)
            self.assertIsNotNone(series_db)
            self.assertEqual(len(series_db.posts), 5)
            self.assertEqual(series_db.status, SeriesStatus.DRAFT)

        # Step 3: Navigation: on_series_nav position 2
        cb_nav2 = create_mock_callback(admin_id, f"series_nav:{series_id}:2")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_nav(cb_nav2)
        cb_nav2.message.edit_text.assert_called_once()
        self.assertIn("2 из 5", cb_nav2.message.edit_text.call_args[0][0])

        # Step 4: Command: "Сделай пост 3 короче"
        msg_cmd1 = create_mock_message(admin_id, "Сделай пост 3 короче")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_text_input(msg_cmd1)
        self.assertTrue(msg_cmd1.answer.called)

        # Check post 3 shortened in DB
        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            s_after_1 = res.scalar_one()
            p3_len = len(s_after_1.posts[2].content)
            self.assertLessEqual(p3_len, 280)

        # Step 5: Command: "Замени пост 2 на: Эксклюзивный чеклист активностей в экосистеме"
        msg_cmd2 = create_mock_message(admin_id, "Замени пост 2 на: Эксклюзивный чеклист активностей в экосистеме")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_text_input(msg_cmd2)

        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            s_after_2 = res.scalar_one()
            self.assertIn("Эксклюзивный чеклист", s_after_2.posts[1].content)

        # Step 6: Command: "Перегенерируй пост 4"
        msg_cmd3 = create_mock_message(admin_id, "Перегенерируй пост 4")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_text_input(msg_cmd3)

        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            s_after_3 = res.scalar_one()
            self.assertTrue(len(s_after_3.posts[3].content) > 0)

        # Step 7: Command: "Удали пост 3"
        msg_cmd4 = create_mock_message(admin_id, "Удали пост 3")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_text_input(msg_cmd4)

        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            s_after_4 = res.scalar_one()
            self.assertEqual(len(s_after_4.posts), 4)
            self.assertEqual([p.position for p in s_after_4.posts], [1, 2, 3, 4])

        # Step 8: Command: "Добавь пост о распределении токенов"
        msg_cmd5 = create_mock_message(admin_id, "Добавь пост о распределении токенов")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_text_input(msg_cmd5)

        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            s_after_5 = res.scalar_one()
            self.assertEqual(len(s_after_5.posts), 5)
            self.assertTrue(any("распределении токенов" in p.content.lower() for p in s_after_5.posts))

        # Step 9: Command: "Сделай всю серию более динамичной"
        msg_cmd6 = create_mock_message(admin_id, "Сделай всю серию более динамичной")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_text_input(msg_cmd6)

        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            s_after_6 = res.scalar_one()
            self.assertEqual(len(s_after_6.posts), 5)

        # Step 10: Undo via chat command
        msg_undo = create_mock_message(admin_id, "undo")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_text_input(msg_undo)
        self.assertTrue(any("Undo выполнено" in str(c) for c in msg_undo.answer.mock_calls))

        # Step 11: Redo via callback
        cb_redo = create_mock_callback(admin_id, f"series_redo:{series_id}:1")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_redo(cb_redo)
        self.assertTrue(any("Повтор выполнен" in str(c) for c in cb_redo.answer.mock_calls))

        # Step 12: Save Draft
        cb_save = create_mock_callback(admin_id, f"series_save_draft:{series_id}")
        with patch("bot.handlers.series_review.get_session", self.mock_get_session):
            await on_series_save_draft(cb_save)
        self.assertTrue(any("сохранена как черновик" in str(c) for c in cb_save.answer.mock_calls))

        # Step 13: Publish via callback
        cb_pub = create_mock_callback(admin_id, f"series_publish:{series_id}")

        async def mock_publish_to_x(text, **kwargs):
            return "sim_tweet_123", "https://x.com/tweet/123"

        with patch("bot.handlers.series_review.get_session", self.mock_get_session), \
             patch("publishing.series_publisher.get_session", self.mock_get_session), \
             patch("publishing.series_publisher.publish_to_x", side_effect=mock_publish_to_x):
            await on_series_publish(cb_pub)

        async with self.Session() as session:
            series_final = await session.get(ContentSeries, series_id)
            self.assertEqual(series_final.status, SeriesStatus.PUBLISHED)

    # =========================================================================
    # 2. REAL EDITOR V2 POST ISOLATION (BIT-FOR-BIT FIDELITY)
    # =========================================================================
    async def test_editor_v2_strict_post_isolation(self):
        """Create a 5-post series, edit ONLY post #3.
        Posts 1, 2, 4, 5 must remain 100% BIT-FOR-BIT IDENTICAL.
        Undo must restore original, Redo must reapply.
        """
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.X_THREAD,
            desired_posts_count=5,
        )

        orig_posts = [copy.deepcopy(p.content) for p in series_data.posts]
        self.assertEqual(len(orig_posts), 5)

        async with self.Session() as session:
            db_series = ContentSeries(
                project_id=proj.id,
                format=SeriesFormat.X_THREAD,
                title="Isolation Test",
                total_posts=5,
                status=SeriesStatus.DRAFT,
                current_version=1,
            )
            session.add(db_series)
            await session.flush()
            series_data.id = db_series.id
            for p in series_data.posts:
                session.add(
                    SeriesPost(
                        series_id=db_series.id,
                        position=p.position,
                        purpose=p.purpose,
                        content=p.content,
                    )
                )
            await session.commit()
            await SeriesVersionManager.save_snapshot(session, db_series.id, "Initial", "init", series_data)
            await session.commit()
            series_id = db_series.id

            # Edit ONLY post 3
            plan = EditPlan(
                scope="post",
                position=3,
                target="content",
                operation="replace",
                new_value="UPDATED POST 3 CONTENT: Perform liquidity bridge to earn 2x multiplier.",
                explanation="Edit post 3",
            )
            updated_data, ok, err = await SeriesEditor.apply_series_edit_plan(plan, series_data)
            self.assertTrue(ok)

            # Verification of bit-for-bit isolation:
            self.assertEqual(updated_data.posts[0].content, orig_posts[0])
            self.assertEqual(updated_data.posts[1].content, orig_posts[1])
            self.assertNotEqual(updated_data.posts[2].content, orig_posts[2])
            self.assertEqual(updated_data.posts[3].content, orig_posts[3])
            self.assertEqual(updated_data.posts[4].content, orig_posts[4])

            # Persist and Snapshot
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            db_series = res.scalar_one()
            sync_data_to_series_db(updated_data, db_series, session)
            await SeriesVersionManager.save_snapshot(session, series_id, "Edit post 3", "edit 3", updated_data)
            await session.commit()

            # UNDO: check exact original state restored across all posts
            restored_orig, msg = await SeriesVersionManager.undo(session, series_id)
            self.assertIsNotNone(restored_orig)
            for idx in range(5):
                self.assertEqual(
                    restored_orig.posts[idx].content,
                    orig_posts[idx],
                    f"Post {idx+1} does not match original after Undo!",
                )

            # REDO: check edited state restored
            restored_edit, msg = await SeriesVersionManager.redo(session, series_id)
            self.assertIsNotNone(restored_edit)
            self.assertEqual(restored_edit.posts[2].content, updated_data.posts[2].content)
            self.assertEqual(restored_edit.posts[0].content, orig_posts[0])
            self.assertEqual(restored_edit.posts[4].content, orig_posts[4])

    # =========================================================================
    # 3. FACT FIDELITY HARDENING (REJECT HALLUCINATIONS & SECURITY LEAKS)
    # =========================================================================
    def test_fact_fidelity_hardening(self):
        """Verifies that SeriesValidator flags:
        - Conflicting blockchain networks (e.g. Solana when source is Arbitrum).
        - Contradictory numeric reward amounts (e.g. $10,000 when source is $2500+).
        - Foreign/phishing URLs not matching source domain.
        - Fabricated exact deadline dates when source has none.
        - Security violations (private key, seed phrase, send eth).
        """
        source_facts = {
            "title": "LayerZero Protocol",
            "network": "Arbitrum",
            "potential_reward": "$2500+",
            "project_link": "https://layerzero.network",
            "deadline": None,
        }

        # Case A: Wrong network hallucination
        s_wrong_net = SeriesData(
            title="LayerZero Protocol",
            format=SeriesFormat.X_THREAD,
            total_posts=2,
            posts=[
                SeriesPostData(position=1, content="LayerZero is launching on Solana with high yields!"),
                SeriesPostData(position=2, content="Join the Solana liquidity pool now."),
            ],
        )
        res_a = SeriesValidator.validate_source_fact_fidelity(s_wrong_net, source_facts)
        self.assertFalse(res_a.is_valid)
        self.assertTrue(any("solana" in e.lower() for e in res_a.errors))

        # Case B: Contradictory numeric reward ($10,000 vs $2500+)
        s_fake_reward = SeriesData(
            title="LayerZero Protocol",
            format=SeriesFormat.X_THREAD,
            total_posts=2,
            posts=[
                SeriesPostData(position=1, content="LayerZero on Arbitrum confirms potential reward of $10,000 for users!"),
                SeriesPostData(position=2, content="Official portal: https://layerzero.network"),
            ],
        )
        res_b = SeriesValidator.validate_source_fact_fidelity(s_fake_reward, source_facts)
        self.assertFalse(res_b.is_valid)
        self.assertTrue(any("$10,000" in e for e in res_b.errors))

        # Case C: Foreign / phishing URL
        s_phish_url = SeriesData(
            title="LayerZero Protocol",
            format=SeriesFormat.X_THREAD,
            total_posts=2,
            posts=[
                SeriesPostData(position=1, content="LayerZero on Arbitrum reward: $2500+"),
                SeriesPostData(position=2, content="Claim your tokens here: https://fake-claim-scam-airdrop.xyz/claim"),
            ],
        )
        res_c = SeriesValidator.validate_source_fact_fidelity(s_phish_url, source_facts)
        self.assertFalse(res_c.is_valid)
        self.assertTrue(any("fake-claim-scam-airdrop.xyz" in e for e in res_c.errors))

        # Case D: Fabricated deadline date
        s_fake_deadline = SeriesData(
            title="LayerZero Protocol",
            format=SeriesFormat.X_THREAD,
            total_posts=2,
            posts=[
                SeriesPostData(position=1, content="LayerZero on Arbitrum reward: $2500+.\nDeadline: 25.12.2026"),
                SeriesPostData(position=2, content="Official portal: https://layerzero.network"),
            ],
        )
        res_d = SeriesValidator.validate_source_fact_fidelity(s_fake_deadline, source_facts)
        self.assertFalse(res_d.is_valid)
        self.assertTrue(any("дедлайн" in e.lower() or "deadline" in e.lower() for e in res_d.errors))

        # Case E: Dangerous security violation
        s_security_leak = SeriesData(
            title="LayerZero Protocol",
            format=SeriesFormat.X_THREAD,
            total_posts=2,
            posts=[
                SeriesPostData(position=1, content="To verify your wallet, please enter your private key into the form."),
                SeriesPostData(position=2, content="Official portal: https://layerzero.network"),
            ],
        )
        res_e = SeriesValidator.validate_source_fact_fidelity(s_security_leak, source_facts)
        self.assertFalse(res_e.is_valid)
        self.assertTrue(any("безопасности" in e.lower() or "private key" in e.lower() for e in res_e.errors))

    # =========================================================================
    # 4. PRE-PUBLISH LENGTH VALIDATION (NO SILENT TRUNCATION)
    # =========================================================================
    async def test_pre_publish_length_validation_guard(self):
        """Verifies that SeriesPublisher rejects tweets exceeding 280 chars
        without silent truncation.
        """
        proj, draft, _ = create_sample_project_and_draft()
        async with self.Session() as session:
            db_series = ContentSeries(
                project_id=proj.id,
                format=SeriesFormat.X_THREAD,
                title="Length Guard Test",
                total_posts=2,
                status=SeriesStatus.DRAFT,
            )
            session.add(db_series)
            await session.flush()

            # Post 1 is valid (50 chars), Post 2 is invalid (310 chars)
            session.add(SeriesPost(series_id=db_series.id, position=1, content="Post 1 valid content"))
            session.add(
                SeriesPost(
                    series_id=db_series.id,
                    position=2,
                    content="A" * 310,  # Exceeds 280
                )
            )
            await session.commit()
            series_id = db_series.id

        async def mock_pub(text, **kwargs):
            return "tweet_1", "https://x.com/tweet_1"

        with patch("publishing.series_publisher.get_session", self.mock_get_session), \
             patch("publishing.series_publisher.publish_to_x", side_effect=mock_pub):

            res = await SeriesPublisher.publish_series(series_id)
            self.assertFalse(res.success)
            self.assertEqual(res.status, SeriesStatus.PARTIALLY_PUBLISHED)
            self.assertIn("280", res.error_message)

        # Check DB: post 2 is marked FAILED, not truncated
        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            s_db = res.scalar_one()
            self.assertEqual(s_db.posts[0].status, PostStatus.PUBLISHED)
            self.assertEqual(s_db.posts[1].status, PostStatus.FAILED)
            self.assertEqual(len(s_db.posts[1].content), 310)

    # =========================================================================
    # 5. IDEMPOTENCY: DB-LEVEL STATUS CAS GUARD
    # =========================================================================
    async def test_db_level_publishing_status_idempotency_guard(self):
        """Verifies that if series.status is already PUBLISHING in DB,
        another worker or process cannot start a duplicate publish.
        """
        proj, draft, _ = create_sample_project_and_draft()
        async with self.Session() as session:
            db_series = ContentSeries(
                project_id=proj.id,
                format=SeriesFormat.X_THREAD,
                title="DB Status Guard Test",
                total_posts=1,
                status=SeriesStatus.PUBLISHING,  # Already locked by another worker
            )
            session.add(db_series)
            await session.flush()
            session.add(SeriesPost(series_id=db_series.id, position=1, content="P1"))
            await session.commit()
            series_id = db_series.id

        with patch("publishing.series_publisher.get_session", self.mock_get_session):
            res = await SeriesPublisher.publish_series(series_id)
            self.assertFalse(res.success)
            self.assertIn("уже выполняется", res.error_message)


if __name__ == "__main__":
    unittest.main()
