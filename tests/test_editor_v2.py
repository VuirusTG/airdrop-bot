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


def test_fast_parse_title_on_photo_variations():
    from services.image_rework import detect_theme_color, requests_text_rework

    draft = get_sample_draft()
    cmd1 = 'Сделай название на фото "Wager Predict"'
    cmd2 = "Поменяй название проекта на фото на Wager Predict"
    cmd3 = "Измени заголовок на фото на: Wager Predict"

    # Theme color must NOT be triggered by "Predict" containing "red"
    assert detect_theme_color(cmd1) is None
    assert detect_theme_color(cmd2) is None
    assert requests_text_rework(cmd1) is True
    assert requests_text_rework(cmd2) is True

    plan1 = _fast_deterministic_parse(cmd1, draft)
    assert plan1 is not None
    assert plan1.target == "project_title"
    assert plan1.new_value == "Wager Predict"
    assert plan1.image_operation == "rerender_text"

    plan2 = _fast_deterministic_parse(cmd2, draft)
    assert plan2 is not None
    assert plan2.target == "project_title"
    assert plan2.new_value == "Wager Predict"
    assert plan2.image_operation == "rerender_text"

    plan3 = _fast_deterministic_parse(cmd3, draft)
    assert plan3 is not None
    assert plan3.target == "project_title"
    assert plan3.new_value == "Wager Predict"
    assert plan3.image_operation == "rerender_text"


def test_detect_theme_color_word_boundaries():
    from services.image_rework import detect_theme_color

    # Substring false positives must be rejected
    assert detect_theme_color("Wager Predict") is None
    assert detect_theme_color("Credit score on Base") is None
    assert detect_theme_color("Shared liquidity pool") is None
    assert detect_theme_color("Redirect user to portal") is None

    # Real color commands must be accepted
    assert detect_theme_color("Сделай фон красным") == "red"
    assert detect_theme_color("Смени тему на синий") == "cyan"
    assert detect_theme_color("Поставь цвет violet") == "violet"


def test_create_edit_plan_llm_first_routing():
    """Verify that when LLM key is present, create_edit_plan uses LLM JSON."""
    from unittest.mock import AsyncMock, patch
    from config import settings

    draft = get_sample_draft()
    mock_llm_plan_json = """{
        "target": "project_title",
        "operation": "replace",
        "old_value": "Flop Network",
        "new_value": "Wager Predict",
        "confidence": 0.99,
        "requires_confirmation": false,
        "affected_components": ["draft_data", "telegram_post", "social_card"],
        "image_operation": "rerender_text",
        "explanation": "Изменение названия проекта на 'Wager Predict'"
    }"""

    loop = asyncio.new_event_loop()
    try:
        with patch.object(settings, "OPENROUTER_API_KEY", "mock_openrouter_key"):
            with patch("services.openrouter_client.generate_json", new=AsyncMock(return_value=mock_llm_plan_json)):
                plan = loop.run_until_complete(
                    EditorService.create_edit_plan("Сделай название на фото 'Wager Predict'", draft)
                )
                assert plan.target == "project_title"
                assert plan.new_value == "Wager Predict"
                assert plan.image_operation == "rerender_text"
                assert plan.confidence == 0.99
    finally:
        loop.close()


def test_create_edit_plan_llm_failover_to_deterministic():
    """Verify that when LLM fails, create_edit_plan falls back to deterministic parsing."""
    from unittest.mock import AsyncMock, patch
    from config import settings

    draft = get_sample_draft()
    loop = asyncio.new_event_loop()
    try:
        with patch.object(settings, "OPENROUTER_API_KEY", "mock_key"):
            with patch("services.openrouter_client.generate_json", new=AsyncMock(side_effect=RuntimeError("OpenRouter timeout"))):
                with patch.object(settings, "GROQ_API_KEY", None):
                    with patch.object(settings, "GEMINI_API_KEY", None):
                        plan = loop.run_until_complete(
                            EditorService.create_edit_plan("Измени Potential Rewards с $2500+ на $1000+", draft)
                        )
                        assert plan.target == "potential_reward"
                        assert plan.new_value == "$1000+"
                        assert plan.image_operation == "rerender_text"
    finally:
        loop.close()


if __name__ == "__main__":
    test_fast_parse_reward_change()
    test_fast_parse_replace_single_task()
    test_fast_parse_remove_task()
    test_fast_parse_add_task()
    test_fast_parse_new_artwork()
    test_task_validator_strict_rules()
    test_task_sanitizer()
    test_fast_parse_title_on_photo_variations()
    test_detect_theme_color_word_boundaries()
    test_create_edit_plan_llm_first_routing()
    test_create_edit_plan_llm_failover_to_deterministic()
    print("ALL EDITOR V2 & TASK VALIDATOR TESTS PASSED!")

