import asyncio
import os
import sys

# Set dummy environment variables for tests before importing config
os.environ.setdefault("BOT_TOKEN", "dummy_bot_token")
os.environ.setdefault("ADMIN_USER_ID", "123456")
os.environ.setdefault("PUBLISH_CHANNEL_ID", "-100123456")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test.db")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.draft_content import DraftContent
from services.editor_service import EditorService, _fast_deterministic_parse
from services.task_validator import sanitize_task, validate_tasks, validate_single_task


def get_sample_draft() -> DraftContent:
    return DraftContent(
        title="Termix AI Hunt",
        category="AIRDROP",
        description="Termix is building decentralized AI agent network.",
        tasks=[
            "Bridge ETH to Base network",
            "Complete early onboarding quest",
            "Hold 50 TEST tokens in wallet",
        ],
        potential_reward="$2500+",
        network="Base",
        project_link="https://termix.ai",
    )


def test_fast_parse_reward_change():
    draft = get_sample_draft()
    plan = _fast_deterministic_parse("Измени Potential Rewards с $2500+ на $1000+", draft)
    assert plan is not None
    assert plan.target == "potential_reward"
    assert plan.operation == "replace"
    assert plan.new_value == "$1000+"
    assert plan.image_operation == "rerender_text"

    # Test applying plan
    loop = asyncio.new_event_loop()
    updated, is_valid, err = loop.run_until_complete(EditorService.apply_edit_plan(plan, draft))
    loop.close()

    assert is_valid is True
    assert updated.potential_reward == "$1000+"
    # Verify title, tasks, network untouched!
    assert updated.title == draft.title
    assert updated.tasks == draft.tasks
    assert updated.network == draft.network


def test_fast_parse_replace_single_task():
    draft = get_sample_draft()
    cmd = "Замени второй пункт на: Claim all available Chips points."
    plan = _fast_deterministic_parse(cmd, draft)
    assert plan is not None
    assert plan.target == "task_2"
    assert plan.operation == "replace"
    assert plan.new_value == "Claim all available Chips points"
    assert plan.image_operation == "rerender_text"

    loop = asyncio.new_event_loop()
    updated, is_valid, err = loop.run_until_complete(EditorService.apply_edit_plan(plan, draft))
    loop.close()

    assert is_valid is True
    assert updated.tasks[1] == "Claim all available Chips points"
    assert updated.tasks[0] == draft.tasks[0]
    assert updated.tasks[2] == draft.tasks[2]
    assert updated.potential_reward == draft.potential_reward


def test_fast_parse_remove_task():
    draft = get_sample_draft()
    cmd = "Удали третий пункт"
    plan = _fast_deterministic_parse(cmd, draft)
    assert plan is not None
    assert plan.target == "task_3"
    assert plan.operation == "remove"

    loop = asyncio.new_event_loop()
    updated, is_valid, err = loop.run_until_complete(EditorService.apply_edit_plan(plan, draft))
    loop.close()

    assert is_valid is True
    assert len(updated.tasks) == 2
    assert updated.tasks == ["Bridge ETH to Base network", "Complete early onboarding quest"]


def test_fast_parse_add_task():
    draft = get_sample_draft()
    cmd = "Добавь задачу: Mint supporter NFT on Zora"
    plan = _fast_deterministic_parse(cmd, draft)
    assert plan is not None
    assert plan.target == "tasks"
    assert plan.operation == "add"
    assert plan.new_value == "Mint supporter NFT on Zora"

    loop = asyncio.new_event_loop()
    updated, is_valid, err = loop.run_until_complete(EditorService.apply_edit_plan(plan, draft))
    loop.close()

    assert is_valid is True
    assert len(updated.tasks) == 4
    assert updated.tasks[3] == "Mint supporter NFT on Zora"


def test_fast_parse_new_artwork():
    draft = get_sample_draft()
    cmd = "Полностью создай новый фон в cyberpunk стиле"
    plan = _fast_deterministic_parse(cmd, draft)
    assert plan is not None
    assert plan.target == "image_background"
    assert plan.operation == "regenerate"
    assert plan.image_operation == "new_artwork"


def test_task_validator_strict_rules():
    # Valid tasks
    valid_tasks = [
        "Connect EVM wallet to Base",
        "Swap 50 USDC on Uniswap",
        "Mint early community badge",
    ]
    res = validate_tasks(valid_tasks)
    assert res.is_valid is True

    # Invalid: contains ellipsis
    res_dots = validate_tasks(["Connect wallet and swap..."])
    assert res_dots.is_valid is False
    assert "ellipsis" in res_dots.error_summary

    # Invalid: contains unicode ellipsis
    res_udots = validate_tasks(["Connect wallet and swap…"])
    assert res_udots.is_valid is False

    # Invalid: contains filler "etc."
    res_etc = validate_tasks(["Mint NFT, stake tokens, etc."])
    assert res_etc.is_valid is False
    assert "filler" in res_etc.error_summary

    # Invalid: over 120 characters
    long_task = "A" * 125
    res_long = validate_tasks([long_task])
    assert res_long.is_valid is False
    assert "120" in res_long.error_summary

    # Invalid: duplicates
    res_dup = validate_tasks(["Bridge ETH", "Bridge ETH"])
    assert res_dup.is_valid is False
    assert "Duplicate" in res_dup.error_summary


def test_task_sanitizer():
    dirty = "1. **Connect** your wallet! 🚀..."
    cleaned = sanitize_task(dirty)
    # Numbering and emojis stripped
    assert not cleaned.startswith("1.")
    assert "🚀" not in cleaned
    assert "**" not in cleaned


if __name__ == "__main__":
    test_fast_parse_reward_change()
    test_fast_parse_replace_single_task()
    test_fast_parse_remove_task()
    test_fast_parse_add_task()
    test_fast_parse_new_artwork()
    test_task_validator_strict_rules()
    test_task_sanitizer()
    print("ALL EDITOR V2 & TASK VALIDATOR TESTS PASSED!")
