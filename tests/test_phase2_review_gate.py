import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

# Configure test environment
os.environ.setdefault("BOT_TOKEN", "dummy_bot_token")
os.environ.setdefault("ADMIN_USER_ID", "123456")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "-100123456")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from db.database import _ensure_optional_columns
from db.models import Base, Draft, DraftSnapshot, Project, ProjectStatus
from services.draft_content import (
    ArtworkMetadata,
    DraftContent,
    LayoutMetadata,
    draft_to_content,
    parse_legacy_instructions,
    sync_content_to_draft,
)
from services.editor_service import EditorService, _fast_deterministic_parse
from services.social_card import render_social_card_from_content
from services.version_manager import VersionManager


class TestPhase2ReviewGate(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _ensure_optional_columns(conn)
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    # -------------------------------------------------------------
    # 1. PostgreSQL Schema & DDL Compatibility Checks
    # -------------------------------------------------------------
    def test_postgresql_ddl_generation(self):
        """Verify that all SQLAlchemy models generate valid PostgreSQL DDL without errors."""
        pg_dialect = postgresql.dialect()
        tables = [Draft.__table__, DraftSnapshot.__table__, Project.__table__]
        for table in tables:
            ddl = str(CreateTable(table).compile(dialect=pg_dialect))
            self.assertTrue(len(ddl) > 0)
            self.assertIn("CREATE TABLE", ddl)
            # Verify specific postgres types
            if table.name == "drafts":
                self.assertIn("content_json TEXT", ddl)
                self.assertIn("edit_plan_json TEXT", ddl)
            if table.name == "draft_snapshots":
                self.assertIn("draft_id INTEGER", ddl)
                self.assertIn("project_id INTEGER", ddl)
                self.assertIn("version INTEGER", ddl)
                self.assertIn("content_json TEXT", ddl)

    async def test_migration_idempotency(self):
        """Verify that running _ensure_optional_columns multiple times is 100% safe and does not fail."""
        async with self.engine.begin() as conn:
            # Run 1st time (already run in setUp)
            await _ensure_optional_columns(conn)
            # Run 2nd time
            await _ensure_optional_columns(conn)
            # Run 3rd time
            await _ensure_optional_columns(conn)
        # If it reached here without exception, idempotency is confirmed.

    # -------------------------------------------------------------
    # 2. Legacy Instructions Parsing Tests on Real Examples
    # -------------------------------------------------------------
    def test_legacy_instructions_real_examples(self):
        """Test parser on real historical formats from scraped airdrops."""
        # Example 1: Multi-line numbered steps with dots
        text1 = (
            "1. Connect your MetaMask wallet to the official portal.\n"
            "2. Bridge at least 0.05 ETH to Arbitrum One.\n"
            "3. Complete the onboarding quest on Layer3.\n"
            "4. Claim your OAT badge on Galxe."
        )
        tasks1, review1 = parse_legacy_instructions(text1)
        self.assertFalse(review1)
        self.assertEqual(len(tasks1), 4)
        self.assertEqual(tasks1[0], "Connect your MetaMask wallet to the official portal")
        self.assertEqual(tasks1[1], "Bridge at least 0.05 ETH to Arbitrum One")
        self.assertEqual(tasks1[2], "Complete the onboarding quest on Layer3")
        self.assertEqual(tasks1[3], "Claim your OAT badge on Galxe")

        # Example 2: Bullet points with boilerplate header
        text2 = (
            "What to do:\n"
            "- Swap USDC to USDT on DEX\n"
            "* Deposit liquidity into the pool\n"
            "• Stake LP tokens for 14 days"
        )
        tasks2, review2 = parse_legacy_instructions(text2)
        self.assertFalse(review2)
        self.assertEqual(len(tasks2), 3)
        self.assertEqual(tasks2[0], "Swap USDC to USDT on DEX")
        self.assertEqual(tasks2[1], "Deposit liquidity into the pool")
        self.assertEqual(tasks2[2], "Stake LP tokens for 14 days")

        # Example 3: Single paragraph with embedded numbering (e.g. "1. Step A 2. Step B 3. Step C")
        text3 = "1. Visit the portal. 2. Verify Twitter and Discord. 3. Mint the early supporter NFT."
        tasks3, review3 = parse_legacy_instructions(text3)
        self.assertFalse(review3)
        self.assertEqual(len(tasks3), 3)
        self.assertEqual(tasks3[0], "Visit the portal")
        self.assertEqual(tasks3[1], "Verify Twitter and Discord")
        self.assertEqual(tasks3[2], "Mint the early supporter NFT")

    def test_legacy_instructions_unparseable_preserves_without_hallucinating(self):
        """Test that unparseable instructions NEVER invent fake tasks."""
        # Gibberish / too short
        unparseable = "xyz ???"
        tasks, needs_review = parse_legacy_instructions(unparseable)
        self.assertEqual(tasks, [])
        self.assertTrue(needs_review)

        # Empty
        empty_tasks, empty_review = parse_legacy_instructions("")
        self.assertEqual(empty_tasks, [])
        self.assertTrue(empty_review)

        # Draft conversion of unparseable retains original raw instructions
        legacy_draft = Draft(
            id=10,
            project_id=1,
            version=1,
            title="Weird Project",
            summary="Some summary",
            instructions="xyz ???",
            potential_reward=None,
        )
        content = draft_to_content(legacy_draft)
        self.assertEqual(content.tasks, [])
        self.assertTrue(content.needs_review)
        self.assertEqual(content.raw_instructions_fallback, "xyz ???")
        # Ensure no hallucinated placeholder text like "VISIT THE OFFICIAL..."
        self.assertNotIn("VISIT THE OFFICIAL", content.render_telegram_post())

    # -------------------------------------------------------------
    # 3. Image Generation Isolation (Levels 1, 2, 3)
    # -------------------------------------------------------------
    @patch("services.artwork_generator.generate_artwork", new_callable=AsyncMock)
    async def test_reward_edit_does_not_call_image_generation(self, mock_generate_artwork):
        """Changing reward must use Level 1 (rerender_text) and NEVER call generate_artwork."""
        content = DraftContent(
            title="LayerZero",
            category="AIRDROP",
            description="Interoperability protocol.",
            tasks=["Bridge OFT tokens across 3 chains", "Hold STG in voting escrow"],
            potential_reward="$2500+",
            network="Arbitrum",
            project_link="https://layerzero.network",
        )
        plan = _fast_deterministic_parse("Измени reward с $2500+ на $1000+", content)
        self.assertEqual(plan.target, "potential_reward")
        self.assertEqual(plan.image_operation, "rerender_text")

        updated, is_valid, _ = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)
        self.assertEqual(updated.potential_reward, "$1000+")

        # Verify generate_artwork was NEVER invoked
        mock_generate_artwork.assert_not_called()

    @patch("services.artwork_generator.generate_artwork", new_callable=AsyncMock)
    async def test_task_edit_does_not_call_image_generation(self, mock_generate_artwork):
        """Changing task must use Level 1 (rerender_text) and NEVER call generate_artwork."""
        content = DraftContent(
            title="Starknet Quest",
            category="AIRDROP",
            description="ZK Rollup on Ethereum.",
            tasks=["Setup Argent X wallet", "Swap ETH on JediSwap", "Mint domain on Starknet ID"],
            potential_reward="$1500+",
            network="Ethereum",
            project_link="https://starknet.io",
        )
        plan = _fast_deterministic_parse("Замени второй пункт на: Provide liquidity on Ekubo", content)
        self.assertEqual(plan.target, "task_2")
        self.assertEqual(plan.image_operation, "rerender_text")

        updated, is_valid, _ = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)
        self.assertEqual(updated.tasks[1], "Provide liquidity on Ekubo")

        # Verify generate_artwork was NEVER invoked
        mock_generate_artwork.assert_not_called()

    async def test_artwork_edit_preserves_post_content(self):
        """Changing artwork must NOT touch title, tasks, reward, network or links."""
        content = DraftContent(
            title="Zora Network",
            category="AIRDROP",
            description="L2 for creators and artists.",
            tasks=["Bridge ETH to Zora", "Mint 3 trending open editions", "Create a collection"],
            potential_reward="$800+",
            network="Base",
            project_link="https://zora.co",
        )
        plan = _fast_deterministic_parse("Полностью создай новый фон в стиле cyber neon", content)
        self.assertEqual(plan.target, "image_background")
        self.assertEqual(plan.image_operation, "new_artwork")

        updated, is_valid, _ = await EditorService.apply_edit_plan(plan, content)
        self.assertTrue(is_valid)

        # Post fields must remain strictly identical
        self.assertEqual(updated.title, "Zora Network")
        self.assertEqual(updated.description, content.description)
        self.assertEqual(updated.tasks, content.tasks)
        self.assertEqual(updated.potential_reward, "$800+")
        self.assertEqual(updated.network, "Base")
        self.assertEqual(updated.project_link, "https://zora.co")

    # -------------------------------------------------------------
    # 4. VersionManager & Undo After Two Consecutive Edits
    # -------------------------------------------------------------
    async def test_version_manager_two_consecutive_edits_and_undos(self):
        """Apply Edit 1, then Edit 2. Verify 1st Undo reverts Edit 2, and 2nd Undo reverts Edit 1."""
        async with self.Session() as session:
            # 1. Setup project & draft
            project = Project(
                dedup_hash="dedup-hash-scroll",
                name="Scroll",
                chain="Ethereum",
                category="AIRDROP",
                source="test",
                status=ProjectStatus.PENDING_REVIEW,
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)

            initial_content = DraftContent(
                title="Scroll zkEVM",
                category="AIRDROP",
                description="Scroll native zkEVM layer 2.",
                tasks=["Bridge ETH to Scroll", "Swap on Ambient", "Deploy smart contract"],
                potential_reward="$2000+",
                network="Ethereum",
                project_link="https://scroll.io",
            )

            draft = Draft(
                project_id=project.id,
                version=1,
                title=initial_content.title,
                summary=initial_content.description,
                instructions="\n".join(f"{i}. {t}" for i, t in enumerate(initial_content.tasks, 1)),
                potential_reward=initial_content.potential_reward,
                content_json=initial_content.to_json(),
            )
            session.add(draft)
            await session.commit()
            await session.refresh(draft)

            # Baseline snapshot v1
            await VersionManager.save_snapshot(
                session, draft.id, project.id,
                action="Исходный черновик",
                user_command=None,
                edit_plan_json=None,
                content=initial_content,
            )

            # --- EDIT 1: Change reward to $3500+ ---
            plan1 = _fast_deterministic_parse("Измени reward с $2000+ на $3500+", initial_content)
            content_v2, is_valid, _ = await EditorService.apply_edit_plan(plan1, initial_content)
            self.assertTrue(is_valid)
            sync_content_to_draft(content_v2, draft)
            draft.version = 2
            await VersionManager.save_snapshot(
                session, draft.id, project.id,
                action=plan1.explanation,
                user_command="Измени reward на $3500+",
                edit_plan_json=None,
                content=content_v2,
            )

            # Verify state after Edit 1
            self.assertEqual(draft.potential_reward, "$3500+")
            self.assertEqual(draft.version, 2)

            # --- EDIT 2: Change task 1 ---
            plan2 = _fast_deterministic_parse("Замени первый пункт на: Bridge via official Scroll bridge", content_v2)
            content_v3, is_valid2, _ = await EditorService.apply_edit_plan(plan2, content_v2)
            self.assertTrue(is_valid2)
            sync_content_to_draft(content_v3, draft)
            draft.version = 3
            await VersionManager.save_snapshot(
                session, draft.id, project.id,
                action=plan2.explanation,
                user_command="Замени первый пункт",
                edit_plan_json=None,
                content=content_v3,
            )

            # Verify state after Edit 2
            self.assertEqual(draft.potential_reward, "$3500+")
            self.assertEqual(content_v3.tasks[0], "Bridge via official Scroll bridge")
            self.assertEqual(draft.version, 3)

            # --- UNDO 1: Should revert Edit 2, leaving Edit 1 in place ---
            restored_1, msg1 = await VersionManager.undo(session, draft.id)
            self.assertIsNotNone(restored_1)
            self.assertEqual(restored_1.tasks[0], "Bridge ETH to Scroll")  # reverted
            self.assertEqual(restored_1.potential_reward, "$3500+")        # still from Edit 1
            self.assertEqual(draft.potential_reward, "$3500+")

            # Still can undo because baseline snapshot v1 is available!
            can_undo_again = await VersionManager.can_undo(session, draft.id)
            self.assertTrue(can_undo_again)

            # --- UNDO 2: Should revert Edit 1 back to baseline ---
            restored_2, msg2 = await VersionManager.undo(session, draft.id)
            self.assertIsNotNone(restored_2)
            self.assertEqual(restored_2.potential_reward, "$2000+")        # baseline restored!
            self.assertEqual(draft.potential_reward, "$2000+")
            self.assertEqual(restored_2.tasks[0], "Bridge ETH to Scroll")

            # --- UNDO 3: Cannot undo further ---
            can_undo_third = await VersionManager.can_undo(session, draft.id)
            self.assertFalse(can_undo_third)
            restored_3, msg3 = await VersionManager.undo(session, draft.id)
            self.assertIsNone(restored_3)
            self.assertIn("Нет предыдущих версий", msg3)

    # -------------------------------------------------------------
    # 5. Shared Single Source of Truth
    # -------------------------------------------------------------
    async def test_shared_draft_content_between_telegram_and_social_card(self):
        """Telegram post and social card renderers must consume the exact same DraftContent."""
        content = DraftContent(
            title="Berachain Artio",
            category="TESTNET",
            description="EVM-compatible Layer 1 blockchain built on Proof-of-Liquidity.",
            tasks=[
                "Drip testnet BERA from faucet",
                "Swap BERA for STGUSDC on BEX",
                "Mint HONEY on Honey dApp",
            ],
            potential_reward="$1200+",
            network="Berachain",
            project_link="https://artio.faucet.berachain.com",
        )

        # 1. Telegram post
        tg_post = content.render_telegram_post()
        self.assertIn("Berachain Artio", tg_post)
        self.assertIn("1. Drip testnet BERA from faucet.", tg_post)
        self.assertIn("2. Swap BERA for STGUSDC on BEX.", tg_post)
        self.assertIn("3. Mint HONEY on Honey dApp.", tg_post)
        self.assertIn("$1200+", tg_post)
        self.assertIn("Berachain", tg_post)

        # 2. Social card (Level 1)
        card = await render_social_card_from_content(content)
        self.assertIsNotNone(card)
        self.assertTrue(os.path.exists(card.path))
        self.assertGreater(os.path.getsize(card.path), 2000)


if __name__ == "__main__":
    unittest.main()
