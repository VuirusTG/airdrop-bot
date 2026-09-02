"""Deterministic filtering and drafting used while cloud AI is unavailable."""
from __future__ import annotations

import re
from html import unescape

from services.llm_draft import DraftResult
from services.llm_filter import FilterResult


OPPORTUNITY_MARKERS = {
    "airdrop": ("airdrop", "air drop", "retrodrop", "retroactive"),
    "testnet": ("testnet", "test net", "faucet", "incentivized"),
    "quest": ("quest", "campaign", "mission", "galxe", "zealy"),
    "points": ("points", "point program", "xp", "season"),
    "waitlist": ("waitlist", "early access"),
}
ACTION_MARKERS = (
    "live", "launched", "open", "join", "claim", "complete", "participate",
    "earn", "register", "mint", "bridge", "swap", "stake", "deposit",
)
CRITICAL_RISK_MARKERS = (
    "seed phrase", "recovery phrase", "private key", "guaranteed return",
    "guaranteed profit", "pay to unlock", "send funds to claim",
)
EDITORIAL_MARKERS = (
    "price prediction", "technical analysis", "market update", "weekly update",
    "what is an airdrop", "airdrop explained",
)
CHAIN_MARKERS = {
    "Ethereum": ("ethereum", "erc-20"),
    "Solana": ("solana",),
    "Arbitrum": ("arbitrum",),
    "Optimism": ("optimism", "op mainnet"),
    "Base": ("base network", "base chain"),
    "Starknet": ("starknet",),
    "zkSync": ("zksync",),
    "Cosmos": ("cosmos", "ibc"),
}


def _plain_text(value: str) -> str:
    text = unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _category(text: str) -> str:
    for category, markers in OPPORTUNITY_MARKERS.items():
        if any(marker in text for marker in markers):
            return category
    return "other"


def _chain(text: str) -> str | None:
    for chain, markers in CHAIN_MARKERS.items():
        if any(marker in text for marker in markers):
            return chain
    return None


def fallback_score_project(name: str, raw_text: str) -> FilterResult:
    """Apply a conservative keyword filter and disclose its lower confidence."""
    text = f"{name}\n{raw_text}".lower()
    category = _category(text)
    is_opportunity = category != "other"
    has_current_action = any(marker in text for marker in ACTION_MARKERS)
    critical_risk = any(marker in text for marker in CRITICAL_RISK_MARKERS)
    editorial_only = any(marker in text for marker in EDITORIAL_MARKERS) and not has_current_action

    score = 2.0 + (2.5 if is_opportunity else 0.0) + (1.5 if has_current_action else 0.0)
    if critical_risk:
        score = 0.0
    elif editorial_only:
        score = min(score, 2.5)

    passes = is_opportunity and has_current_action and not critical_risk and not editorial_only
    reasoning = (
        "Локальный режим без AI: найдены признаки актуальной активности; "
        "легитимность, сроки и ссылки необходимо проверить вручную."
        if passes
        else "Локальный режим без AI: нет достаточно явных признаков актуального действия или обнаружен риск."
    )
    return FilterResult(
        score=score,
        verdict="review" if passes else "reject",
        reasoning=reasoning,
        chain=_chain(text),
        category=category,
        is_opportunity=is_opportunity,
        has_current_action=has_current_action,
        confidence="low",
        critical_risk=critical_risk,
    )


def _source_excerpt(raw_text: str, limit: int = 260) -> str:
    text = _plain_text(raw_text)
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "."


def _x_post(name: str, category: str, project_url: str | None) -> str:
    label = {
        "airdrop": "airdrop opportunity", "testnet": "testnet",
        "quest": "quest campaign", "points": "points campaign",
        "waitlist": "early-access opportunity",
    }.get(category, "crypto opportunity")
    suffix = f"\n\n{project_url}" if project_url else ""
    text = (
        f"{name} has a new {label} to review.\n\nRewards are unconfirmed. "
        "Verify the official page, use a separate wallet, and check every transaction before signing."
        "\n\nWorth exploring? #airdrop"
    )
    available = 280 - len(suffix)
    if len(text) > available:
        text = text[: max(0, available - 1)].rsplit(" ", 1)[0].rstrip(".,;:") + "."
    return text + suffix


def fallback_generate_draft(
    name: str,
    raw_text: str,
    chain: str | None,
    category: str,
    project_url: str | None,
) -> DraftResult:
    """Build an English, non-inventive draft suitable for manual review."""
    context = _source_excerpt(raw_text)
    ecosystem = f" in the {chain} ecosystem" if chain else ""
    summary = (
        f"{name} appears to have a new {category} opportunity{ecosystem}. "
        f"The source reports: {context} "
        "This draft was created without AI, so confirm all details on the official page before publishing."
    )
    instructions = "\n".join((
        "1. Open the official project page using the link below.",
        "2. Verify that the campaign is active and review its eligibility rules.",
        "3. Follow only the tasks listed by the project on its official page.",
        "4. Use a separate wallet and verify every transaction before signing.",
    ))
    return DraftResult(
        title=f"{name}: New {category.title()} Opportunity",
        summary=summary,
        instructions=instructions,
        potential_reward="No reward or token allocation is confirmed. Participation may not lead to an airdrop.",
        risk_note="Verify the domain and official accounts; never share a seed phrase or private key.",
        twitter_text=_x_post(name, category, project_url),
        image_prompt=(
            f"A polished 16:9 editorial crypto visual for {name}{ecosystem}, representing a {category} campaign, "
            "clean geometric composition, high contrast, ample empty space for a headline, no readable text, "
            "no financial promises, no fake interface, no invented partner logos"
        ),
    )
