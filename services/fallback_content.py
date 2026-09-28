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


def _clean_project_name(name: str) -> str:
    """Strip redundant suffixes like 'Airdrop', 'Testnet', 'Campaign' from project name."""
    clean = re.sub(r"(?i)\s+(?:airdrop|testnet|campaign|quest|retrodrop|bounty)$", "", (name or "").strip())
    return clean.strip() or name or "Project"


def _deduplicate_phrases(text: str) -> str:
    """Remove consecutive duplicate words or phrases (e.g. 'IRIS Credit IRIS Credit')."""
    if not text:
        return ""
    # Strip consecutive identical words
    text = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", text, flags=re.IGNORECASE)
    # Strip 2, 3, 4-word repeated phrases
    for n in (4, 3, 2):
        pattern = r"\b((?:\w+\s+){" + str(n - 1) + r"}\w+)\s+\1\b"
        text = re.sub(pattern, r"\1", text, flags=re.IGNORECASE)
    return text.strip()


def _extract_reward_info(raw_text: str) -> str:
    """Extract real token tickers, reward pools, or XP from text instead of contradictory boilerplate."""
    text = raw_text or ""
    # 1. Search for token ticker like $MBK, $LIT, $DEI, $IRIS
    token_match = re.search(r"\$([A-Z0-9]{2,10})\b", text)
    token = f"${token_match.group(1)}" if token_match else None

    # Search for token mentioned without $ (e.g. "MBK token", "IRIS airdrop")
    if not token:
        alt_token = re.search(r"\b([A-Z0-9]{2,8})\s+(?:token|tokens|airdrop)\b", text)
        if alt_token:
            cand = alt_token.group(1).upper()
            if cand not in {"THE", "NEW", "GET", "FOR", "AND", "OUR", "ALL", "NOT", "FREE", "BEST", "REAL", "AIRDROP", "POOL", "TEST"}:
                token = f"${cand}"

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
    """Extract actionable task steps from raw text or build project-specific ones without any raw URLs."""
    clean_name = _clean_project_name(name)
    clean = _plain_text(raw_text)
    # Strip any URLs, links, or URL parents
    clean = re.sub(r"https?://\S+", "", clean)
    clean = re.sub(r"\([^)]*https?://[^)]*\)", "", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    tasks: list[str] = []

    # Search for action sentences or clauses in raw_text
    candidates = re.split(r"[.;\n]+", clean)
    action_keywords = ("earn", "post", "engage", "tip", "trade", "swap", "bridge", "mint", "stake", "deposit", "participate", "collect", "borrow", "lend", "supply", "faucet", "claim")
    for cand in candidates:
        cand_str = cand.strip()
        cand_str = re.sub(r"^\s*(\d+[\.\)]|[-*•])\s*", "", cand_str).strip()
        if len(cand_str) < 15 or len(cand_str) > 85:
            continue
        words = cand_str.lower().split()
        if any(w in words or any(cand_str.lower().startswith(ak) for ak in action_keywords) for w in action_keywords):
            # Clean up into an imperative or clear step
            step = cand_str
            step = re.sub(r"^(where\s+users\s+|users\s+|you\s+can\s+|and\s+|to\s+|please\s+)", "", step, flags=re.IGNORECASE).strip()
            # Double check no URLs slipped in
            step = re.sub(r"https?://\S+", "", step).strip(" ()[]")
            if step and len(step) >= 12 and not any(step.lower() in t.lower() for t in tasks):
                tasks.append(step[0].upper() + step[1:])
        if len(tasks) >= 3:
            break

    # If insufficient dynamic tasks extracted from text, construct tailored high-quality archetype steps
    if len(tasks) < 2:
        lower_all = f"{clean_name} {raw_text} {category}".lower()
        if any(w in lower_all for w in ("testnet", "devnet", "faucet", "sepolia", "goerli")):
            tasks = [
                "Claim testnet tokens from the official faucet.",
                "Execute smart contract interactions and platform features.",
                "Submit feedback and wallet confirmation on official channels.",
                "Maintain test activity to qualify for the community distribution.",
            ]
        elif any(w in lower_all for w in ("credit", "lend", "borrow", "collateral", "debt", "loan", "vault")):
            tasks = [
                "Connect your Web3 wallet to the protocol interface.",
                "Deposit collateral and test active lending or borrowing pools.",
                "Accumulate interaction volume to increase early distribution share.",
                "Track points and allocation standings on the dashboard.",
            ]
        elif any(w in lower_all for w in ("dex", "swap", "amm", "liquidity", "perps", "trade", "trading", "orderbook")):
            tasks = [
                "Connect your wallet to the decentralized exchange dApp.",
                "Execute swaps across designated trading pairs.",
                "Provide liquidity to eligible pools to earn reward shares.",
                "Monitor your trading score on the rewards leaderboard.",
            ]
        elif any(w in lower_all for w in ("stake", "staking", "validator", "node", "restake", "delegat")):
            tasks = [
                "Connect your wallet and choose an active staking pool.",
                "Stake native assets to begin accruing protocol points.",
                "Maintain active stake during the eligibility snapshot period.",
                "Check the rewards dashboard for claim and distribution updates.",
            ]
        elif any(w in lower_all for w in ("form", "waitlist", "survey", "google", "application", "whitelist")):
            tasks = [
                "Submit the official access form and verify your wallet address.",
                "Complete initial verification and onboarding requirements.",
                "Accumulate early supporter points and track announcements.",
                "Stay active in the community for whitelist and claim access.",
            ]
        else:
            cat_display = category.title() if category else "Ecosystem"
            tasks = [
                f"Connect your Web3 wallet to the official {clean_name} platform.",
                f"Complete active onboarding actions and {cat_display.lower()} requirements.",
                "Accumulate points, XP, or protocol interactions for early allocation.",
                "Monitor official channels for snapshot dates and claim details.",
            ]
    elif len(tasks) == 2:
        tasks.append("Track points and allocation progress on the official portal.")

    # Ensure max 4 concise tasks without any trailing URLs or junk
    return tasks[:4]


def _build_engaging_description(name: str, raw_text: str, chain: str | None, category: str) -> str:
    """Format description with clean 2-paragraph spacing and zero repetitive wall-of-text."""
    clean_name = _clean_project_name(name)
    clean = _plain_text(raw_text)
    clean = re.sub(r"https?://\S+", "", clean)
    clean = _deduplicate_phrases(clean)

    # Strip known boilerplate
    clean = re.sub(r"This draft was created without AI[^\.]*\.?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"The source reports:\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"appears to have a new[^\.]*\.?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Extract informative sentences
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean) if len(s.strip()) > 20]
    meaningful = [
        s for s in sentences
        if not any(bp in s.lower() for bp in ("without ai", "source reports", "confirm all details", "verify the domain", "never share a seed"))
    ]

    cleaned_sentences = []
    for s in meaningful:
        s_dedup = _deduplicate_phrases(s)
        # Deduplicate duplicated leading project name
        pattern = rf"^(?:{re.escape(clean_name)}\s*){{2,}}"
        s_dedup = re.sub(pattern, f"{clean_name} ", s_dedup, flags=re.IGNORECASE).strip()
        cleaned_sentences.append(s_dedup)

    eco = f" on {chain}" if chain else ""
    cat_title = category.title() if category else "Ecosystem"

    if cleaned_sentences:
        p1 = cleaned_sentences[0]
        if not p1.lower().startswith(clean_name.lower()):
            p1 = f"{clean_name} is {p1[0].lower() + p1[1:]}" if not p1.startswith(("A ", "An ", "The ")) else f"{clean_name}: {p1}"

        if len(cleaned_sentences) > 1:
            p2 = cleaned_sentences[1]
        else:
            p2 = f"The {clean_name} team has opened early access{eco} for community participants to test the platform and earn ecosystem allocation."
    else:
        p1 = f"{clean_name} has launched its {cat_title.lower()} campaign{eco}, introducing on-chain participation mechanics for early supporters."
        p2 = "Active participants can interact with protocol features, complete early milestones, and qualify for upcoming community allocations."

    return f"{p1}\n\n{p2}"


def _x_post(name: str, category: str, project_url: str | None, reward: str, chain: str | None, tasks: list[str]) -> str:
    """Construct an engaging Twitter/X post strictly <= 280 characters with hook, bullets, and link."""
    clean_name = _clean_project_name(name)
    eco = f" #{chain}" if chain else ""
    link_str = f" {project_url}" if project_url else ""

    t1 = tasks[0] if tasks else "Connect wallet & complete tasks"
    t1 = re.sub(r"https?://\S+", "", t1).strip(" .:,")
    if len(t1) > 38:
        t1 = t1[:36].rsplit(" ", 1)[0] + "…"

    t2 = tasks[1] if len(tasks) > 1 else "Accumulate early allocation"
    t2 = re.sub(r"https?://\S+", "", t2).strip(" .:,")
    if len(t2) > 38:
        t2 = t2[:36].rsplit(" ", 1)[0] + "…"

    reward_clean = reward.replace("Token Allocation & Airdrop Rewards", "Allocation").replace("Early Community & Ecosystem Allocation", "Early Allocation")
    if len(reward_clean) > 32:
        reward_clean = reward_clean[:30].rsplit(" ", 1)[0] + "…"

    # Standard high-converting structure
    tweet = (
        f"🪂 {clean_name} Airdrop is live{eco}!\n\n"
        f"💰 {reward_clean}\n\n"
        f"1⃣ {t1}\n"
        f"2⃣ {t2}\n\n"
        f"🔗 Farm here:{link_str}\n#airdrop #crypto"
    )

    if len(tweet) <= 280:
        return tweet

    # Compact format if link is long
    compact = (
        f"🪂 {clean_name} Airdrop live{eco}!\n\n"
        f"💰 {reward_clean}\n"
        f"• {t1}\n"
        f"• {t2}\n\n"
        f"🔗 Join:{link_str} #airdrop"
    )
    if len(compact) <= 280:
        return compact

    # Ultra-compact fallback
    available = 280 - len(link_str) - 20
    head = f"🪂 {clean_name} Airdrop live{eco}! 💰 {reward_clean}"[:available]
    return f"{head}\n🔗{link_str} #airdrop"


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

