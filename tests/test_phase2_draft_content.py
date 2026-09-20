"""Tests for Phase 2: Unified DraftContent model, backward compatibility & legacy parsing."""
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.draft_content import (
    ArtworkMetadata,
    DraftContent,
    LayoutMetadata,
    draft_to_content,
    parse_legacy_instructions,
    sync_content_to_draft,
)


def test_draft_content_roundtrip():
    content = DraftContent(
        title="zkSync Era Airdrop",
        category="AIRDROP",
        description="Major Layer-2 zkRollup scaling Ethereum with zero knowledge cryptography.",
        tasks=[
            "Bridge ETH to zkSync Era mainnet",
            "Swap assets on SyncSwap or Mute",
            "Provide liquidity in any verified pool",
        ],
        potential_reward="$2500+",
        network="zkSync",
        project_link="https://zksync.io",
        links=["https://zksync.io"],
        twitter="@JanjezCrypto",
        telegram="@janjezcrypto",
        risk_note="High gas fees during network congestion",
        twitter_text="zkSync is live. Check your eligibility #zksync",
        source_url="https://x.com/zksync",
        artwork=ArtworkMetadata(
            path="images/generated/test.jpg",
            source="generated_social_card_master",
            preset="kunoichi",
            theme_color="cyan",
        ),
        layout=LayoutMetadata(card_style_version="ninja-scout-cyber-v10"),
    )

    json_str = content.to_json()
    restored = DraftContent.from_json(json_str)

    assert restored.title == content.title
    assert restored.category == "AIRDROP"
    assert restored.description == content.description
    assert len(restored.tasks) == 3
    assert restored.tasks[0] == "Bridge ETH to zkSync Era mainnet"
    assert restored.potential_reward == "$2500+"
    assert restored.network == "zkSync"
    assert restored.artwork.theme_color == "cyan"
    assert restored.artwork.preset == "kunoichi"


def test_parse_legacy_instructions_numbered():
    legacy_text = (
        "1. Connect your Web3 wallet to the platform.\n"
        "2. Swap at least 50 USDC for ETH.\n"
        "3. Mint the early community badge on-chain."
    )
    tasks, needs_review = parse_legacy_instructions(legacy_text)
    assert len(tasks) == 3
    assert tasks[0] == "Connect your Web3 wallet to the platform"
    assert tasks[1] == "Swap at least 50 USDC for ETH"
    assert tasks[2] == "Mint the early community badge on-chain"
    assert not needs_review


def test_parse_legacy_instructions_bullets():
    legacy_text = (
        "Instructions:\n"
        "- Bridge funds to Arbitrum\n"
        "- Complete quest on Layer3\n"
        "- Claim discord role"
    )
    tasks, needs_review = parse_legacy_instructions(legacy_text)
    assert len(tasks) == 3
    assert tasks[0] == "Bridge funds to Arbitrum"
    assert tasks[1] == "Complete quest on Layer3"
    assert tasks[2] == "Claim discord role"
    assert not needs_review


def test_parse_legacy_instructions_empty_or_broken():
    tasks, needs_review = parse_legacy_instructions("")
    assert tasks == []
    assert needs_review is True

    tasks, needs_review = parse_legacy_instructions("What to do:\n")
    assert tasks == []
    assert needs_review is True


def test_deterministic_telegram_rendering():
    content = DraftContent(
        title="Optimism Superchain",
        description="Scaling Ethereum with OP Stack rollups.",
        tasks=[
            "Bridge to OP Mainnet",
            "Vote on active governance proposals",
        ],
        potential_reward="$1500+",
        network="Optimism",
        project_link="https://optimism.io",
    )

    rendered = content.render_telegram_post()
    assert "🚀 Optimism Superchain" in rendered
    assert "Scaling Ethereum with OP Stack rollups." in rendered
    assert "📝 What to do:" in rendered
    assert "1. Bridge to OP Mainnet." in rendered
    assert "2. Vote on active governance proposals." in rendered
    assert "💰 Potential reward: $1500+" in rendered
    assert "🌐 Network: Optimism" in rendered
    assert "🔗 Start here: https://optimism.io" in rendered
    assert "..." not in rendered
    assert "…" not in rendered


class DummyProject:
    id = 42
    name = "Test Project"
    chain = "Base"
    category = "TESTNET"
    project_url = "https://base.org"
    source_url = "https://x.com/base"


class DummyDraft:
    id = 101
    title = "Base Testnet Hunt"
    summary = "Explore new contracts on Base testnet."
    instructions = "1. Request testnet ETH.\n2. Deploy smart contract."
    potential_reward = "$3000+"
    risk_note = "Testnet faucet dry"
    twitter_text = "Base is launching #base"
    image_path = "images/generated/base.jpg"
    image_source = "master"
    image_prompt = "cyber base artwork"
    source_url = "https://x.com/base"
    project_url = "https://base.org"
    content_json = None


def test_legacy_draft_adaptation_and_sync():
    project = DummyProject()
    draft = DummyDraft()

    # Adapt legacy draft to DraftContent
    content = draft_to_content(draft, project)
    assert content.title == "Base Testnet Hunt"
    assert content.network == "Base"
    assert len(content.tasks) == 2
    assert content.tasks[0] == "Request testnet ETH"
    assert content.tasks[1] == "Deploy smart contract"

    # Sync DraftContent back to Draft
    content.potential_reward = "$1000+"
    content.tasks.append("Verify contract on explorer")
    sync_content_to_draft(content, draft)

    assert draft.content_json is not None
    assert draft.potential_reward == "$1000+"
    assert "3. Verify contract on explorer." in draft.instructions

    # Re-reading from draft uses content_json now
    content2 = draft_to_content(draft, project)
    assert content2.potential_reward == "$1000+"
    assert len(content2.tasks) == 3


if __name__ == "__main__":
    test_draft_content_roundtrip()
    test_parse_legacy_instructions_numbered()
    test_parse_legacy_instructions_bullets()
    test_parse_legacy_instructions_empty_or_broken()
    test_deterministic_telegram_rendering()
    test_legacy_draft_adaptation_and_sync()
    print("ALL PHASE 2 TESTS PASSED SUCCESSFULLY!")
