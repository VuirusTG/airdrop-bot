import asyncio
import os
import sys
import tempfile
import unittest

# Configure test environment
os.environ.setdefault("BOT_TOKEN", "dummy_bot_token")
os.environ.setdefault("ADMIN_USER_ID", "123456")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "-100123456")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from db.models import Base, Draft, DraftSnapshot, Project, ProjectStatus
from services.draft_content import DraftContent, draft_to_content, sync_content_to_draft
from services.editor_service import EditorService, _fast_deterministic_parse
from services.task_validator import validate_tasks, validate_single_task, sanitize_task
from services.version_manager import VersionManager
from services.social_card import render_social_card_from_content


def get_sample_content() -> DraftContent:
    return DraftContent(
        title="zkSync Era Portal",
        category="AIRDROP",
        description="zkSync is a Layer 2 rollup that uses zero-knowledge proofs.",
        tasks=[
            "Bridge ETH to zkSync Era mainnet",
            "Swap tokens on SyncSwap DEX",
            "Supply liquidity in any pool",
        ],
        potential_reward="$2500+",
        network="Ethereum",
        project_link="https://zksync.io",
    )


class TestEditorV2Full(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    # 1. Test Deterministic NL Edits (Reward, Tasks, Network, Title, Artwork)
    async def test_reward_edit_preserves_other_fields(self):
        content = get_sample_content()
        plan = _fast_deterministic_parse("Измени Potential Rewards с $2500+ на $1000+", content)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.target, "potential_reward")
        self.assertEqual(plan.new_value, "$1000+")
        self.assertEqual(plan.image_operation, "rerender_text")

        updated, is_valid, err = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)
        self.assertEqual(updated.potential_reward, "$1000+")
        # Critical: check that tasks, title, network, link remain completely unchanged
        self.assertEqual(updated.title, content.title)
        self.assertEqual(updated.tasks, content.tasks)
        self.assertEqual(updated.network, content.network)
        self.assertEqual(updated.project_link, content.project_link)

    async def test_single_task_edit(self):
        content = get_sample_content()
        cmd = "Замени второй пункт на: Execute 5 volume swaps on Maverick"
        plan = _fast_deterministic_parse(cmd, content)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.target, "task_2")

        updated, is_valid, err = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)
        self.assertEqual(len(updated.tasks), 3)
        self.assertEqual(updated.tasks[0], "Bridge ETH to zkSync Era mainnet")
        self.assertEqual(updated.tasks[1], "Execute 5 volume swaps on Maverick")
        self.assertEqual(updated.tasks[2], "Supply liquidity in any pool")
        self.assertEqual(updated.potential_reward, content.potential_reward)

    async def test_remove_and_add_task(self):
        content = get_sample_content()
        # Remove task 3
        plan_rm = _fast_deterministic_parse("Удали третий шаг", content)
        self.assertIsNotNone(plan_rm)
        updated, is_valid, _ = await EditorService.apply_edit_plan(plan_rm, content)
        self.assertTrue(is_valid)
        self.assertEqual(len(updated.tasks), 2)
        self.assertEqual(updated.tasks, [
            "Bridge ETH to zkSync Era mainnet",
            "Swap tokens on SyncSwap DEX",
        ])

        # Add new task
        plan_add = _fast_deterministic_parse("Добавь шаг: Mint free NFT on MintSquare", updated)
        self.assertIsNotNone(plan_add)
        updated2, is_valid2, _ = await EditorService.apply_edit_plan(plan_add, updated)
        self.assertTrue(is_valid2)
        self.assertEqual(len(updated2.tasks), 3)
        self.assertEqual(updated2.tasks[2], "Mint free NFT on MintSquare")

    async def test_new_artwork_command(self):
        content = get_sample_content()
        cmd = "Полностью создай новый фон в cyberpunk neon стиле"
        plan = _fast_deterministic_parse(cmd, content)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.target, "image_background")
        self.assertEqual(plan.image_operation, "new_artwork")

        updated, is_valid, _ = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)
        # Content tasks and reward untouched
        self.assertEqual(updated.tasks, content.tasks)
        self.assertEqual(updated.potential_reward, content.potential_reward)

    # 2. Strict Task Validation & Anti-Slicing
    def test_strict_task_validation_rules(self):
        # 1. No ellipsis
        res1 = validate_tasks(["Do step 1...", "Do step 2"])
        self.assertFalse(res1.is_valid)
        self.assertIn("ellipsis", res1.error_summary.lower())

        # 2. No filler words
        res2 = validate_tasks(["Do step 1 and more", "Do step 2"])
        self.assertFalse(res2.is_valid)
        self.assertIn("filler", res2.error_summary.lower())

        # 3. Length <= 120
        long_task = "A" * 125
        res3 = validate_tasks([long_task])
        self.assertFalse(res3.is_valid)
        self.assertIn("120", res3.error_summary)

        # 4. Sanitizer removes numbering and emojis but preserves clean text
        sanitized = sanitize_task("1. 🔥 **Connect** wallet to app...")
        self.assertFalse(sanitized.startswith("1."))
        self.assertNotIn("🔥", sanitized)
        self.assertNotIn("**", sanitized)

    def test_deterministic_telegram_post_rendering(self):
        content = get_sample_content()
        post = content.render_telegram_post()
        self.assertIn("zkSync Era Portal", post)
        self.assertIn("$2500+", post)
        self.assertIn("1. Bridge ETH to zkSync Era mainnet", post)
        self.assertIn("2. Swap tokens on SyncSwap DEX", post)
        self.assertIn("3. Supply liquidity in any pool", post)
        self.assertNotIn("...", post)
        self.assertNotIn("…", post)

    # 3. Level 1 Social Card Generation
    async def test_social_card_level1_render(self):
        content = get_sample_content()
        card = await render_social_card_from_content(content)
        self.assertIsNotNone(card)
        self.assertTrue(os.path.exists(card.path))
        self.assertGreater(os.path.getsize(card.path), 1000)

    # 4. Version Manager & Undo Functionality
    async def test_version_manager_snapshots_and_undo(self):
        async with self.Session() as session:
            # Create project and draft
            project = Project(
                dedup_hash="test-dedup-hash-zksync",
                name="zkSync",
                chain="Ethereum",
                category="AIRDROP",
                source="test",
                status=ProjectStatus.PENDING_REVIEW,
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)

            content_v1 = get_sample_content()
            draft = Draft(
                project_id=project.id,
                version=1,
                title=content_v1.title,
                summary=content_v1.description,
                instructions="\n".join(f"{i}. {t}" for i, t in enumerate(content_v1.tasks, 1)),
                potential_reward=content_v1.potential_reward,
                content_json=content_v1.to_json(),
            )
            session.add(draft)
            await session.commit()
            await session.refresh(draft)

            # Baseline snapshot v1
            snap1 = await VersionManager.save_snapshot(
                session=session,
                draft_id=draft.id,
                project_id=project.id,
                action="Исходный черновик",
                user_command=None,
                edit_plan_json=None,
                content=content_v1,
            )
            self.assertEqual(snap1.version, 1)

            # Check can_undo (only 1 snapshot -> should be False)
            can_undo_1 = await VersionManager.can_undo(session, draft.id)
            self.assertFalse(can_undo_1)

            # Apply edit (change reward to $5000+)
            plan = _fast_deterministic_parse("Измени Potential Rewards с $2500+ на $5000+", content_v1)
            content_v2, is_valid, _ = await EditorService.apply_edit_plan(plan, content_v1)
            self.assertTrue(is_valid)

            sync_content_to_draft(content_v2, draft)
            draft.version = 2
            snap2 = await VersionManager.save_snapshot(
                session=session,
                draft_id=draft.id,
                project_id=project.id,
                action=plan.explanation,
                user_command="Измени reward на $5000+",
                edit_plan_json=None,
                content=content_v2,
            )
            self.assertEqual(snap2.version, 2)

            # Check can_undo (2 snapshots -> should be True)
            can_undo_2 = await VersionManager.can_undo(session, draft.id)
            self.assertTrue(can_undo_2)
            self.assertEqual(draft.potential_reward, "$5000+")

            # Perform Undo
            restored, msg = await VersionManager.undo(session, draft.id)
            self.assertIsNotNone(restored)
            self.assertIn("Отменено", msg)
            self.assertEqual(restored.potential_reward, "$2500+")
            self.assertEqual(draft.potential_reward, "$2500+")

            # After undo, only 1 snapshot remains -> can_undo should be False
            can_undo_3 = await VersionManager.can_undo(session, draft.id)
            self.assertFalse(can_undo_3)

    # 5. Backward Compatibility With Legacy Drafts
    def test_legacy_draft_compatibility(self):
        project = Project(
            name="Legacy Drop",
            chain="Base",
            category="AIRDROP",
        )
        legacy_draft = Draft(
            id=99,
            project_id=1,
            version=1,
            title="Old Legacy Airdrop",
            summary="This is an old summary without content_json",
            instructions="1. Go to website\n2. Connect wallet\n3. Bridge some ETH",
            potential_reward="$1500+",
            content_json=None,  # No JSON stored previously!
        )

        content = draft_to_content(legacy_draft, project)
        self.assertEqual(content.title, "Old Legacy Airdrop")
        self.assertEqual(content.potential_reward, "$1500+")
        self.assertEqual(len(content.tasks), 3)
        self.assertEqual(content.tasks[0], "Go to website")
        self.assertEqual(content.tasks[1], "Connect wallet")
        self.assertEqual(content.tasks[2], "Bridge some ETH")

        # Test rendered_text uses DraftContent seamlessly
        rendered = legacy_draft.rendered_text()
        self.assertIn("Old Legacy Airdrop", rendered)
        self.assertIn("1. Go to website", rendered)


if __name__ == "__main__":
    unittest.main()
