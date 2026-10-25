"""Comprehensive test suite for Content Series & X/Twitter Threads Engine.

Covers all 15 required scenarios:
1. 5-post Series Creation
2. X Thread Creation & Character Limits
3. Single Post Edit ("Сделай пост 3 короче") & Post Isolation
4. Single Post Regenerate ("Перегенерируй пост 2") & Isolation
5. Entire Series Edit ("Сделай всю серию более динамичной")
6. Delete Post & Sequence Renumbering
7. Insert Post & Sequence Renumbering
8. VersionManager Undo after Two Consecutive Edits
9. VersionManager Redo
10. Partial X Publish Recovery & Reply Chain Continuity
11. Double Publish Click Idempotency Lock
12. Strict Anti-Truncation (No [:280], No ellipsis)
13. Source Fact Fidelity (Preserve Reward, Network, Tasks)
14. Dynamic Telegram Preview Verification
15. Existing Editor V2 Scope & Intent Parsing Integration
"""
import asyncio
from contextlib import asynccontextmanager
import copy
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "dummy_bot_token")
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
from services.series_engine import (
    SeriesEngine,
    SeriesPlanner,
    SeriesWriter,
)
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


def create_sample_project_and_draft() -> tuple[Project, Draft, DraftContent]:
    proj = Project(
        id=42,
        dedup_hash="hash_layerzero",
        name="LayerZero V2 Final",
        chain="Arbitrum",
        category="AIRDROP",
        source="github",
        source_url="https://github.com/layerzero",
        project_url="https://layerzero.network",
        status=ProjectStatus.PENDING_REVIEW,
    )
    content = DraftContent(
        title="LayerZero V2 Final Push",
        category="AIRDROP",
        description="LayerZero V2 is entering the final checkpoint phase before snapshot.",
        tasks=[
            "Bridge assets across 5 different chains",
            "Send OFT transaction via Stargate",
            "Hold active wallet activity for 3 distinct months",
        ],
        potential_reward="$2500+",
        network="Arbitrum",
        project_link="https://layerzero.network",
    )
    draft = Draft(
        id=101,
        project_id=42,
        version=1,
    )
    sync_content_to_draft(content, draft)
    proj.drafts = [draft]
    return proj, draft, content


class TestSeriesEngine(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        await self.engine.dispose()
        _active_series_locks.clear()

    # -------------------------------------------------------------
    # TEST 1: Creation of 5-post Series
    # -------------------------------------------------------------
    async def test_01_creation_of_5_post_series(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.SERIES,
            desired_posts_count=5,
        )

        self.assertEqual(len(series_data.posts), 5)
        self.assertEqual(series_data.total_posts, 5)
        self.assertEqual([p.position for p in series_data.posts], [1, 2, 3, 4, 5])
        self.assertIsNotNone(series_data.plan)
        self.assertEqual(len(series_data.plan.posts), 5)
        self.assertTrue(series_data.title)
        self.assertEqual(series_data.format, SeriesFormat.SERIES)

        validation = SeriesValidator.validate_series(series_data)
        self.assertTrue(validation.is_valid, msg=f"Validation errors: {validation.errors}")

    # -------------------------------------------------------------
    # TEST 2: Creation of X Thread & Length Checks
    # -------------------------------------------------------------
    async def test_02_creation_of_x_thread_length_and_quality(self):
        proj, draft, _ = create_sample_project_and_draft()
        thread_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.X_THREAD,
            desired_posts_count=5,
        )

        self.assertEqual(thread_data.format, SeriesFormat.X_THREAD)
        self.assertEqual(len(thread_data.posts), 5)

        for post in thread_data.posts:
            # Must strictly pass 280 characters limit
            self.assertLessEqual(
                len(post.content),
                280,
                f"Post #{post.position} exceeds 280 chars: length={len(post.content)}",
            )
            # Must not end with trailing ellipsis
            self.assertFalse(post.content.strip().endswith("..."))
            self.assertFalse(post.content.strip().endswith("…"))
            self.assertNotIn("etc.", post.content.lower())

        val = SeriesValidator.validate_series(thread_data)
        self.assertTrue(val.is_valid, msg=f"Errors: {val.errors}")

    # -------------------------------------------------------------
    # TEST 3: Edit Single Post ("Сделай пост 3 короче") & Strict Post Isolation
    # -------------------------------------------------------------
    async def test_03_edit_single_post_shorten_isolation(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.X_THREAD,
            desired_posts_count=5,
        )

        orig_post_1 = copy.deepcopy(series_data.posts[0])
        orig_post_2 = copy.deepcopy(series_data.posts[1])
        orig_post_3 = copy.deepcopy(series_data.posts[2])
        orig_post_4 = copy.deepcopy(series_data.posts[3])
        orig_post_5 = copy.deepcopy(series_data.posts[4])

        plan = EditPlan(
            scope="post",
            position=3,
            target="content",
            operation="shorten",
            explanation="Сокращение поста #3",
        )

        updated_data, ok, err = await SeriesEditor.apply_series_edit_plan(plan, series_data)
        self.assertTrue(ok)
        self.assertIsNone(err)

        # Post 3 must be modified
        self.assertNotEqual(updated_data.posts[2].content, orig_post_3.content)
        self.assertLess(len(updated_data.posts[2].content), len(orig_post_3.content))

        # Posts 1, 2, 4, 5 must remain 100% BIT-FOR-BIT IDENTICAL!
        self.assertEqual(updated_data.posts[0].content, orig_post_1.content)
        self.assertEqual(updated_data.posts[1].content, orig_post_2.content)
        self.assertEqual(updated_data.posts[3].content, orig_post_4.content)
        self.assertEqual(updated_data.posts[4].content, orig_post_5.content)

        val = SeriesValidator.validate_single_post(updated_data.posts[2], SeriesFormat.X_THREAD)
        self.assertTrue(val.is_valid)

    # -------------------------------------------------------------
    # TEST 4: Single Post Regenerate ("Перегенерируй пост 2") & Isolation
    # -------------------------------------------------------------
    async def test_04_regenerate_single_post_isolation(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.X_THREAD,
            desired_posts_count=5,
        )

        p1_before = series_data.posts[0].content
        p2_before = series_data.posts[1].content
        p3_before = series_data.posts[2].content
        p4_before = series_data.posts[3].content
        p5_before = series_data.posts[4].content

        plan = EditPlan(
            scope="post",
            position=2,
            target="post",
            operation="regenerate",
            explanation="Перегенерация поста #2",
        )

        updated_data, ok, _ = await SeriesEditor.apply_series_edit_plan(plan, series_data)
        self.assertTrue(ok)

        # Post 2 updated
        self.assertTrue(len(updated_data.posts[1].content) > 0)
        # Posts 1, 3, 4, 5 unchanged
        self.assertEqual(updated_data.posts[0].content, p1_before)
        self.assertEqual(updated_data.posts[2].content, p3_before)
        self.assertEqual(updated_data.posts[3].content, p4_before)
        self.assertEqual(updated_data.posts[4].content, p5_before)

    # -------------------------------------------------------------
    # TEST 5: Entire Series Edit ("Сделай всю серию более динамичной")
    # -------------------------------------------------------------
    async def test_05_entire_series_edit(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.X_THREAD,
            desired_posts_count=5,
        )

        plan = EditPlan(
            scope="series",
            position=None,
            target="series",
            operation="rewrite",
            new_value="Сделай всю серию более динамичной и дерзкой",
            explanation="Переработка всей серии",
        )

        updated_data, ok, _ = await SeriesEditor.apply_series_edit_plan(plan, series_data)
        self.assertTrue(ok)
        self.assertEqual(len(updated_data.posts), 5)
        self.assertEqual([p.position for p in updated_data.posts], [1, 2, 3, 4, 5])
        val = SeriesValidator.validate_series(updated_data)
        self.assertTrue(val.is_valid)

    # -------------------------------------------------------------
    # TEST 6: Delete Post & Sequence Renumbering
    # -------------------------------------------------------------
    async def test_06_delete_post_and_renumber(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.SERIES,
            desired_posts_count=5,
        )

        orig_post_4_content = series_data.posts[3].content
        orig_post_5_content = series_data.posts[4].content

        plan = EditPlan(
            scope="post",
            position=3,
            target="post",
            operation="remove",
            explanation="Удаление поста #3",
        )

        updated_data, ok, _ = await SeriesEditor.apply_series_edit_plan(plan, series_data)
        self.assertTrue(ok)
        self.assertEqual(len(updated_data.posts), 4)
        self.assertEqual(updated_data.total_posts, 4)
        self.assertEqual([p.position for p in updated_data.posts], [1, 2, 3, 4])

        # Former post 4 is now post 3
        self.assertEqual(updated_data.posts[2].content, orig_post_4_content)
        # Former post 5 is now post 4
        self.assertEqual(updated_data.posts[3].content, orig_post_5_content)

    # -------------------------------------------------------------
    # TEST 7: Insert Post & Sequence Renumbering
    # -------------------------------------------------------------
    async def test_07_insert_post_and_renumber(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.SERIES,
            desired_posts_count=5,
        )

        orig_post_3_content = series_data.posts[2].content

        plan = EditPlan(
            scope="post",
            position=3,
            target="post",
            operation="add",
            new_value="Новый пост о распределении токенов и дедлайнах активности.",
            explanation="Вставка нового поста на позицию #3",
        )

        updated_data, ok, _ = await SeriesEditor.apply_series_edit_plan(plan, series_data)
        self.assertTrue(ok)
        self.assertEqual(len(updated_data.posts), 6)
        self.assertEqual(updated_data.total_posts, 6)
        self.assertEqual([p.position for p in updated_data.posts], [1, 2, 3, 4, 5, 6])

        # New post is at position 3
        self.assertIn("распределении токенов", updated_data.posts[2].content)
        # Former post 3 is now at position 4
        self.assertEqual(updated_data.posts[3].content, orig_post_3_content)

    # -------------------------------------------------------------
    # TEST 8: Undo after Two Consecutive Edits
    # -------------------------------------------------------------
    async def test_08_undo_after_two_consecutive_edits(self):
        proj, draft, _ = create_sample_project_and_draft()
        async with self.Session() as session:
            db_series = ContentSeries(
                project_id=proj.id,
                format=SeriesFormat.X_THREAD,
                title="Test Series Undo",
                topic="Airdrop Guide",
                total_posts=5,
                status=SeriesStatus.DRAFT,
                current_version=1,
            )
            session.add(db_series)
            await session.flush()

            series_data = await SeriesEngine.generate_series(
                project=proj,
                draft=draft,
                format_type=SeriesFormat.X_THREAD,
                desired_posts_count=5,
            )
            series_data.id = db_series.id
            for p in series_data.posts:
                db_p = SeriesPost(
                    series_id=db_series.id,
                    position=p.position,
                    purpose=p.purpose,
                    content=p.content,
                )
                session.add(db_p)
            await session.commit()

            # Snapshot 1: initial
            await SeriesVersionManager.save_snapshot(
                session, db_series.id, "Initial", "init", series_data
            )
            await session.commit()

            # Edit 1: Post 2
            plan1 = EditPlan(
                scope="post", position=2, target="content", operation="replace",
                new_value="EDIT 1: Новое содержание второго поста", explanation="Edit 1"
            )
            s_data1, _, _ = await SeriesEditor.apply_series_edit_plan(plan1, series_data)
            sync_data_to_series_db(s_data1, db_series, session)
            await SeriesVersionManager.save_snapshot(
                session, db_series.id, "Edit 1", "edit 2", s_data1
            )
            await session.commit()

            # Edit 2: Post 4
            plan2 = EditPlan(
                scope="post", position=4, target="content", operation="replace",
                new_value="EDIT 2: Новое содержание четвертого поста", explanation="Edit 2"
            )
            s_data2, _, _ = await SeriesEditor.apply_series_edit_plan(plan2, s_data1)
            sync_data_to_series_db(s_data2, db_series, session)
            await SeriesVersionManager.save_snapshot(
                session, db_series.id, "Edit 2", "edit 4", s_data2
            )
            await session.commit()

            # State after Edit 2
            self.assertIn("EDIT 1", s_data2.posts[1].content)
            self.assertIn("EDIT 2", s_data2.posts[3].content)

            # UNDO 1: Should roll back Edit 2 and restore state after Edit 1
            restored_1, _ = await SeriesVersionManager.undo(session, db_series.id)
            self.assertIsNotNone(restored_1)
            self.assertIn("EDIT 1", restored_1.posts[1].content)
            self.assertNotIn("EDIT 2", restored_1.posts[3].content)

            # UNDO 2: Should roll back Edit 1 and restore initial original state
            restored_0, _ = await SeriesVersionManager.undo(session, db_series.id)
            self.assertIsNotNone(restored_0)
            self.assertNotIn("EDIT 1", restored_0.posts[1].content)
            self.assertNotIn("EDIT 2", restored_0.posts[3].content)

    # -------------------------------------------------------------
    # TEST 9: Redo Restores Undone State
    # -------------------------------------------------------------
    async def test_09_redo_restores_state(self):
        proj, draft, _ = create_sample_project_and_draft()
        async with self.Session() as session:
            db_series = ContentSeries(
                project_id=proj.id,
                format=SeriesFormat.SERIES,
                title="Test Redo",
                topic="Test Topic",
                total_posts=3,
                current_version=1,
            )
            session.add(db_series)
            await session.flush()

            series_data = await SeriesEngine.generate_series(
                project=proj, draft=draft, format_type=SeriesFormat.SERIES, desired_posts_count=3
            )
            series_data.id = db_series.id
            for p in series_data.posts:
                db_p = SeriesPost(
                    series_id=db_series.id,
                    position=p.position,
                    purpose=p.purpose,
                    content=p.content,
                )
                session.add(db_p)
            await session.commit()

            await SeriesVersionManager.save_snapshot(
                session, db_series.id, "Init", "init", series_data
            )
            await session.commit()

            # Edit: Post 1
            plan = EditPlan(
                scope="post", position=1, target="content", operation="replace",
                new_value="BRAND NEW POST 1 HOOK", explanation="Hook change"
            )
            s_data1, _, _ = await SeriesEditor.apply_series_edit_plan(plan, series_data)
            sync_data_to_series_db(s_data1, db_series, session)
            await SeriesVersionManager.save_snapshot(
                session, db_series.id, "Hook change", "edit 1", s_data1
            )
            await session.commit()

            # Undo
            undone, _ = await SeriesVersionManager.undo(session, db_series.id)
            self.assertIsNotNone(undone)
            self.assertNotIn("BRAND NEW POST 1 HOOK", undone.posts[0].content)

            # Redo
            redone, _ = await SeriesVersionManager.redo(session, db_series.id)
            self.assertIsNotNone(redone)
            self.assertIn("BRAND NEW POST 1 HOOK", redone.posts[0].content)

    # -------------------------------------------------------------
    # TEST 10: Partial X Publish Recovery & Reply Chain Continuity
    # -------------------------------------------------------------
    async def test_10_partial_publish_recovery_and_reply_chain(self):
        proj, draft, _ = create_sample_project_and_draft()
        async with self.Session() as session:
            db_series = ContentSeries(
                project_id=proj.id,
                format=SeriesFormat.X_THREAD,
                title="Partial X Thread",
                total_posts=5,
                status=SeriesStatus.DRAFT,
            )
            session.add(db_series)
            await session.flush()

            for i in range(1, 6):
                post = SeriesPost(
                    series_id=db_series.id,
                    position=i,
                    purpose=f"step_{i}",
                    content=f"Post {i} content text",
                    status=PostStatus.PENDING,
                )
                session.add(post)
            await session.commit()
            series_id = db_series.id

        # Mock X publishing: posts 1..3 succeed, post 4 fails
        call_count = 0
        published_reply_ids = []

        async def mock_publish_to_x(text, image_path=None, reply_to_tweet_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            published_reply_ids.append(reply_to_tweet_id)
            if call_count <= 3:
                return f"tweet_{call_count}", f"https://x.com/tweet_{call_count}"
            raise RuntimeError("X API rate limit on tweet 4")

        with patch("publishing.series_publisher.get_session", self.mock_get_session), \
             patch("publishing.series_publisher.publish_to_x", side_effect=mock_publish_to_x):

            res1 = await SeriesPublisher.publish_series(series_id)
            self.assertFalse(res1.success)
            self.assertEqual(res1.published_count, 3)

        # Check DB state after partial failure
        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            db_s = res.scalar_one()
            self.assertEqual(db_s.status, SeriesStatus.PARTIALLY_PUBLISHED)
            posts = sorted(db_s.posts, key=lambda p: p.position)
            self.assertEqual(posts[0].status, PostStatus.PUBLISHED)
            self.assertEqual(posts[0].x_post_id, "tweet_1")
            self.assertEqual(posts[1].status, PostStatus.PUBLISHED)
            self.assertEqual(posts[1].x_post_id, "tweet_2")
            self.assertEqual(posts[2].status, PostStatus.PUBLISHED)
            self.assertEqual(posts[2].x_post_id, "tweet_3")
            self.assertEqual(posts[3].status, PostStatus.FAILED)
            self.assertEqual(posts[4].status, PostStatus.PENDING)

        # Reply chain check:
        # Post 1: reply_to=None
        # Post 2: reply_to=tweet_1
        # Post 3: reply_to=tweet_2
        # Post 4: reply_to=tweet_3 (attempted and failed)
        self.assertEqual(published_reply_ids, [None, "tweet_1", "tweet_2", "tweet_3"])

        # RETRY: Now simulate posts 4 and 5 succeeding
        published_reply_ids.clear()

        async def mock_publish_to_x_retry(text, image_path=None, reply_to_tweet_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            published_reply_ids.append(reply_to_tweet_id)
            return f"tweet_{call_count}", f"https://x.com/tweet_{call_count}"

        with patch("publishing.series_publisher.get_session", self.mock_get_session), \
             patch("publishing.series_publisher.publish_to_x", side_effect=mock_publish_to_x_retry):

            res2 = await SeriesPublisher.publish_series(series_id)
            self.assertTrue(res2.success)
            self.assertEqual(res2.published_count, 5)

        # Verify retry resumed at post 4:
        # Posts 1..3 were NOT re-published!
        # Post 4 reply_to was "tweet_3"!
        # Post 5 reply_to was "tweet_5" (ID returned by post 4)!
        self.assertEqual(len(published_reply_ids), 2)
        self.assertEqual(published_reply_ids[0], "tweet_3")
        self.assertEqual(published_reply_ids[1], "tweet_5")

        # Verify DB is now fully published
        async with self.Session() as session:
            res = await session.execute(
                select(ContentSeries).options(selectinload(ContentSeries.posts)).where(ContentSeries.id == series_id)
            )
            db_s = res.scalar_one()
            self.assertEqual(db_s.status, SeriesStatus.PUBLISHED)

    # -------------------------------------------------------------
    # TEST 11: Double Publish Click Idempotency Lock
    # -------------------------------------------------------------
    async def test_11_double_publish_idempotency_lock(self):
        proj, draft, _ = create_sample_project_and_draft()
        async with self.Session() as session:
            db_series = ContentSeries(
                project_id=proj.id,
                format=SeriesFormat.X_THREAD,
                title="Idempotency Series",
                topic="Idempotency Topic",
                total_posts=2,
                status=SeriesStatus.DRAFT,
            )
            session.add(db_series)
            await session.flush()
            session.add(SeriesPost(series_id=db_series.id, position=1, content="P1"))
            session.add(SeriesPost(series_id=db_series.id, position=2, content="P2"))
            await session.commit()
            series_id = db_series.id

        delay_event = asyncio.Event()

        async def slow_mock_publish(text, image_path=None, reply_to_tweet_id=None, **kwargs):
            await delay_event.wait()
            return "slow_tweet_id", "https://x.com/slow_tweet_id"

        with patch("publishing.series_publisher.get_session", self.mock_get_session), \
             patch("publishing.series_publisher.publish_to_x", side_effect=slow_mock_publish):

            # Launch task 1
            task1 = asyncio.create_task(SeriesPublisher.publish_series(series_id))
            await asyncio.sleep(0.01)

            # Concurrent call (simulating double click by admin)
            res2 = await SeriesPublisher.publish_series(series_id)
            self.assertFalse(res2.success)
            self.assertIn("уже выполняется", res2.error_message)

            # Unblock task 1
            delay_event.set()
            res1 = await task1
            self.assertTrue(res1.success)

    # -------------------------------------------------------------
    # TEST 12: Strict Anti-Truncation (No [:280], No ellipsis)
    # -------------------------------------------------------------
    async def test_12_strict_anti_truncation_enforcement(self):
        # 320 character string
        long_text = (
            "Arbitrum is launching an unprecedented airdrop incentive program for early participants. "
            "To qualify for the maximum tier, users must bridge liquidity across multiple decentralized "
            "protocols, interact with smart contracts on a weekly basis, and accumulate verifiable activity points. "
            "Be sure to stay active until the final snapshot."
        )
        self.assertGreater(len(long_text), 300)

        # Validator MUST flag it if over 280 chars
        val = SeriesValidator.validate_single_post(
            SeriesPostData(position=1, content=long_text),
            series_format=SeriesFormat.X_THREAD,
        )
        self.assertFalse(val.is_valid)
        self.assertTrue(any("280" in e for e in val.errors))

        # SeriesWriter must condense text cleanly WITHOUT slicing [:280] or adding '...'
        rewritten = await SeriesWriter.rewrite_single_post(
            long_text,
            "Condense for X",
            SeriesFormat.X_THREAD,
        )
        self.assertLessEqual(len(rewritten), 280)
        self.assertFalse(rewritten.endswith("..."))
        self.assertFalse(rewritten.endswith("…"))
        # Must not be a raw slice [:280]
        self.assertNotEqual(rewritten, long_text[:280])

    # -------------------------------------------------------------
    # TEST 13: Source Fact Fidelity
    # -------------------------------------------------------------
    async def test_13_source_fact_fidelity_preservation(self):
        proj, draft, content = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.SERIES,
            desired_posts_count=5,
        )

        all_text = " ".join(p.content for p in series_data.posts)
        # Verified facts from source draft must be present
        self.assertIn(content.network, all_text)
        self.assertIn(content.potential_reward, all_text)

        # Validator source fact check
        val = SeriesValidator.validate_source_fact_fidelity(series_data, content)
        self.assertTrue(val.is_valid, msg=f"Fact validation failed: {val.errors}")

    # -------------------------------------------------------------
    # TEST 14: Dynamic Telegram Preview Verification
    # -------------------------------------------------------------
    async def test_14_telegram_preview_dynamic_generation(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.X_THREAD,
            desired_posts_count=5,
        )

        # Test single post preview
        card_post_3 = series_data.format_single_post_view(position=3)
        self.assertIn("3 из 5", card_post_3)
        self.assertIn(series_data.posts[2].content.strip(), card_post_3)

        # Test full sequential preview
        full_preview = series_data.format_full_preview()
        self.assertIn("ПОЛНЫЙ ПРЕДПРОСМОТР", full_preview)
        for post in series_data.posts:
            self.assertIn(f"[{post.position}/5]", full_preview)
            self.assertIn(post.content.strip(), full_preview)

    # -------------------------------------------------------------
    # TEST 15: Existing Editor V2 Scope & Intent Parsing Integration
    # -------------------------------------------------------------
    async def test_15_editor_integration_scopes_and_parsing(self):
        proj, draft, _ = create_sample_project_and_draft()
        series_data = await SeriesEngine.generate_series(
            project=proj,
            draft=draft,
            format_type=SeriesFormat.X_THREAD,
            desired_posts_count=5,
        )

        # A) Deterministic parse: "Сделай третий пост короче"
        plan_a = _fast_deterministic_series_parse("Сделай третий пост короче", series_data)
        self.assertIsNotNone(plan_a)
        self.assertEqual(plan_a.scope, "post")
        self.assertEqual(plan_a.position, 3)
        self.assertEqual(plan_a.operation, "shorten")

        # B) Deterministic parse: "Замени 2 пост на: Полный чеклист по активностям"
        plan_b = _fast_deterministic_series_parse("Замени второй пост на: Полный чеклист по активностям", series_data)
        self.assertIsNotNone(plan_b)
        self.assertEqual(plan_b.scope, "post")
        self.assertEqual(plan_b.position, 2)
        self.assertEqual(plan_b.operation, "replace")
        self.assertEqual(plan_b.new_value, "Полный чеклист по активностям")

        # C) Deterministic parse: "Удали пост 4"
        plan_c = _fast_deterministic_series_parse("Удали 4 пост", series_data)
        self.assertIsNotNone(plan_c)
        self.assertEqual(plan_c.scope, "post")
        self.assertEqual(plan_c.position, 4)
        self.assertEqual(plan_c.operation, "remove")

        # D) Deterministic parse: "Сделай всю серию более динамичной"
        plan_d = _fast_deterministic_series_parse("Сделай всю серию более динамичной", series_data)
        self.assertIsNotNone(plan_d)
        self.assertEqual(plan_d.scope, "series")
        self.assertIsNone(plan_d.position)
        self.assertEqual(plan_d.operation, "rewrite")


if __name__ == "__main__":
    unittest.main()
