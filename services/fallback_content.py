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


def _extract_reward_info(raw_text: str) -> str:
    """Extract real token tickers, reward pools, or XP from text instead of contradictory boilerplate."""
    text = raw_text or ""
    # 1. Search for token ticker like $MBK, $LIT, $DEI
    token_match = re.search(r"\$([A-Z0-9]{2,10})\b", text)
    token = f"${token_match.group(1)}" if token_match else None

    # 2. Search for pool sizes like $30M, $10M, 100M tokens
    pool_match = re.search(r"(?:pool of\s*|reward pool of\s*|\$)(\d+(?:\.\d+)?\s*(?:[Mm]illion|[Kk]|[Bb]illion|[Mm]|[Bb])|\d+\s*[Mm]illion|\d+\s*[Kk])", text, re.IGNORECASE)
    pool = pool_match.group(1).strip() if pool_match else None

    # 3. Check for specific reward mechanisms
    has_nfts = bool(re.search(r"\b(nft|nfts|creator nfts)\b", text, re.IGNORECASE))
    has_xp = bool(re.search(r"\b(xp|points|point system|leaderboard)\b", text, re.IGNORECASE))
    has_tge = bool(re.search(r"\b(tge|token generation|confirmed airdrop)\b", text, re.IGNORECASE))

    if token and has_nfts:
        return f"{token} Token Airdrop + Creator NFTs"
    if token and pool:
        return f"{token} Allocation ({pool} Pool)"
    if token and has_tge:
        return f"Confirmed {token} Token Airdrop at TGE"
    if token and has_xp:
        return f"{token} Airdrop Allocation (XP System)"
    if token:
        return f"{token} Token Allocation & Airdrop Rewards"
    if pool:
        return f"{pool} Reward Pool"
    if has_xp:
        return "XP & Points Allocation for upcoming TGE"
    if has_nfts:
        return "Exclusive Community NFTs & Early Rewards"
    return "Early Community & Ecosystem Allocation"


def _extract_dynamic_tasks(name: str, raw_text: str, category: str, project_url: str | None) -> list[str]:
    """Extract actionable task steps from raw text or build project-specific ones."""
    clean = _plain_text(raw_text)
    tasks: list[str] = []

    # Search for action sentences or clauses in raw_text
    candidates = re.split(r"[.;\n]+", clean)
    action_keywords = ("earn", "post", "engage", "tip", "trade", "swap", "bridge", "mint", "stake", "deposit", "participate", "collect")
    for cand in candidates:
        cand_str = cand.strip()
        if len(cand_str) < 15 or len(cand_str) > 100:
            continue
        words = cand_str.lower().split()
        if any(w in words or any(cand_str.lower().startswith(ak) for ak in action_keywords) for w in action_keywords):
            # Clean up into an imperative or clear step
            step = cand_str
            step = re.sub(r"^(where\s+users\s+|users\s+|you\s+can\s+|and\s+|to\s+)", "", step, flags=re.IGNORECASE).strip()
            if step and not any(step.lower() in t.lower() for t in tasks):
                tasks.append(step[0].upper() + step[1:])
        if len(tasks) >= 3:
            break

    # If insufficient tasks extracted from text, construct tailored high-quality steps
    if len(tasks) < 2:
        tasks = [
            f"Open the official {name} portal{' (' + project_url + ')' if project_url else ''} and connect your wallet.",
            f"Interact with the platform to complete active {category} requirements.",
            "Accumulate points, XP, or test activity to secure your allocation.",
            "Check the project dashboard regularly for snapshot and claim updates.",
        ]
    elif len(tasks) == 2:
        tasks.append("Track your points and allocation on the official dashboard.")

    # Ensure max 4 concise tasks
    return tasks[:4]


def _build_engaging_description(name: str, raw_text: str, chain: str | None, category: str) -> str:
    """Format description with clean paragraph spacing and zero robotic boilerplate."""
    clean = _plain_text(raw_text)
    # Strip known boilerplate
    clean = re.sub(r"This draft was created without AI[^\.]*\.?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"The source reports:\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"appears to have a new[^\.]*\.?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Extract informative sentences
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean) if len(s.strip()) > 20]
    meaningful = [s for s in sentences if not any(bp in s.lower() for bp in ("without ai", "source reports", "confirm all details", "verify the domain"))]

    eco = f" on {chain}" if chain else ""
    p1 = f"🚀 **{name}** is launching its {category} campaign{eco}."
    if meaningful:
        p2 = " ".join(meaningful[:2])
    else:
        p2 = f"{name} introduces community participation mechanics with active incentives for early users."

    return f"{p1}\n\n{p2}"


def _x_post(name: str, category: str, project_url: str | None, reward: str, chain: str | None, tasks: list[str]) -> str:
    """Construct a high-engagement Twitter post strictly <= 280 characters with hook, bullets, and link."""
    eco = f" #{chain}" if chain else ""
    link_str = f" {project_url}" if project_url else ""
    t1 = tasks[0] if tasks else "Complete platform tasks"
    if len(t1) > 40:
        t1 = t1[:38].rsplit(" ", 1)[0] + "…"

    t2 = tasks[1] if len(tasks) > 1 else "Earn rewards"
    if len(t2) > 40:
        t2 = t2[:38].rsplit(" ", 1)[0] + "…"

    # Try full tweet format
    tweet = (
        f"🪂 {name} Airdrop is live{eco}!\n\n"
        f"💰 {reward}\n\n"
        f"• {t1}\n"
        f"• {t2}\n\n"
        f"🔗 Farm here:{link_str} #airdrop"
    )

    if len(tweet) <= 280:
        return tweet

    # Compact format if over 280 chars
    compact_tweet = (
        f"🪂 {name} Airdrop live{eco}!\n\n"
        f"💰 {reward}\n"
        f"👉 Join:{link_str}\n#airdrop"
    )
    if len(compact_tweet) <= 280:
        return compact_tweet

    available = 280 - len(link_str) - 10
    short_header = f"🪂 {name} Airdrop live!"[:available]
    return f"{short_header}\n👉{link_str} #airdrop"


def fallback_generate_draft(
    name: str,
    raw_text: str,
    chain: str | None,
    category: str,
    project_url: str | None,
) -> DraftResult:
    """Build an engaging English draft with dynamic tasks, real rewards, and zero boilerplate."""
    summary = _build_engaging_description(name, raw_text, chain, category)
    tasks = _extract_dynamic_tasks(name, raw_text, category, project_url)
    instructions = "\n".join(f"{idx}. {t.rstrip('.') + '.'}" for idx, t in enumerate(tasks, start=1))
    reward = _extract_reward_info(raw_text)
    tw_text = _x_post(name, category, project_url, reward, chain, tasks)
    ecosystem = f" in the {chain} ecosystem" if chain else ""

    return DraftResult(
        title=f"🔥 {name}: {category.title()} Opportunity",
        summary=summary,
        instructions=instructions,
        potential_reward=reward,
        risk_note=None,
        twitter_text=tw_text,
        image_prompt=(
            f"A polished 16:9 editorial crypto visual for {name}{ecosystem}, representing a {category} campaign, "
            "clean geometric composition, high contrast, ample empty space for a headline, no readable text, "
            "no financial promises, no fake interface, no invented partner logos"
        ),
    )

