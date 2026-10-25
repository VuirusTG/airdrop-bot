"""Series and Thread validator enforcing strict quality, length, and factuality rules."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from services.series_models import SeriesData, SeriesFormat, SeriesPostData


from urllib.parse import urlparse

@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def error_summary(self) -> str:
        return "; ".join(self.errors)


AI_PLACEHOLDER_PATTERNS = [
    r"\[insert\b",
    r"\[link\b",
    r"\[url\b",
    r"\[project\b",
    r"\[name\b",
    r"as an ai\b",
    r"this draft was created without ai",
    r"the source reports:",
    r"tbd\b",
    r"todo\b",
]

SILENT_TRUNCATION_PATTERNS = [
    r"\.{3,}",       # Three or more dots
    r"…",            # Unicode ellipsis
    r"\betc\.?",     # etc / etc.
    r"\band more\b",
    r"\band so on\b",
    r"\bи т\.д\.?",
    r"\bи тому подобное\b",
]

ALLOWED_PUBLIC_DOMAINS = {
    "twitter.com",
    "x.com",
    "t.me",
    "telegram.me",
    "github.com",
    "discord.gg",
    "discord.com",
    "medium.com",
    "mirror.xyz",
    "youtube.com",
    "gitbook.io",
    "linktr.ee",
    "debank.com",
    "coingecko.com",
    "coinmarketcap.com",
}

DANGEROUS_ACTION_PATTERNS = [
    r"\bprivate\s+key\b",
    r"\bseed\s+phrase\b",
    r"\bсид[- ]?фраз\w*\b",
    r"\bприватн\w*\s+ключ\w*\b",
    r"\bsend\s+(?:\d+[\d.]*\s+)?(?:eth|sol|bnb|btc|usdt)\s+to\b",
    r"\bотправ(?:ьте|и)\s+(?:\d+[\d.]*\s+)?(?:эфир|sol|bnb|btc|usdt)\s+на\b",
]


class SeriesValidator:
    """Strict validator for content series and X threads.

    Returns PASS/FAIL + structured errors. Never silently truncates or rewrites text.
    """

    @classmethod
    def validate_single_post(cls, post: SeriesPostData, series_format: str) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        text = (post.content or "").strip()
        if not text:
            errors.append(f"Пост #{post.position} пуст")
            return ValidationResult(is_valid=False, errors=errors, warnings=warnings)

        # 1. Length checks
        if series_format == SeriesFormat.X_THREAD:
            if len(text) > 280:
                errors.append(
                    f"Пост #{post.position} превышает лимит X/Twitter: {len(text)} > 280 символов"
                )
        elif series_format == SeriesFormat.SERIES:
            if len(text) > 4096:
                errors.append(
                    f"Пост #{post.position} превышает лимит Telegram: {len(text)} > 4096 символов"
                )

        # 2. AI Placeholders
        for pat in AI_PLACEHOLDER_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                errors.append(
                    f"Пост #{post.position} содержит шаблонный маркер ИИ ('{pat}')"
                )

        # 3. Silent Truncation markers
        for pat in SILENT_TRUNCATION_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                errors.append(
                    f"Пост #{post.position} содержит маркер обрезки/заполнитель ('{pat}')"
                )

        # 4. Dangerous / Phishing actions
        for pat in DANGEROUS_ACTION_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                errors.append(
                    f"Пост #{post.position} содержит опасное указание безопасности ('{pat}')"
                )

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    @classmethod
    def validate_series(cls, series: SeriesData) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Post count check
        if not series.posts:
            errors.append("Серия не содержит постов")
            return ValidationResult(is_valid=False, errors=errors)

        if len(series.posts) != series.total_posts:
            errors.append(
                f"Несовпадение количества постов: в плане {series.total_posts}, фактически {len(series.posts)}"
            )

        # 2. Consecutive positions check (1..N)
        expected_positions = list(range(1, len(series.posts) + 1))
        actual_positions = [p.position for p in series.posts]
        if actual_positions != expected_positions:
            errors.append(
                f"Нарушена нумерация постов: ожидалось {expected_positions}, получено {actual_positions}"
            )

        # 3. Individual post validations
        texts: list[str] = []
        for post in series.posts:
            res = cls.validate_single_post(post, series.format)
            if not res.is_valid:
                errors.extend(res.errors)
            warnings.extend(res.warnings)
            texts.append(post.content.strip().lower())

        # 4. Duplicate content check across posts
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                if texts[i] == texts[j] and len(texts[i]) > 20:
                    errors.append(f"Посты #{i+1} и #{j+1} содержат одинаковый текст (дубликат)")

        # 5. Source fact fidelity check
        if series.plan and series.plan.source_context:
            fact_res = cls.validate_source_fact_fidelity(series, series.plan.source_context)
            errors.extend(fact_res.errors)
            warnings.extend(fact_res.warnings)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    @classmethod
    def validate_source_fact_fidelity(
        cls,
        series: SeriesData,
        source_content: Any,
    ) -> ValidationResult:
        """Verifies that key source facts (network, reward, links, deadlines) are preserved without hallucinations."""
        errors: list[str] = []
        warnings: list[str] = []
        raw_full_text = " ".join(p.content for p in series.posts)
        full_text = raw_full_text.lower()

        def _get_val(key: str) -> Any:
            if isinstance(source_content, dict):
                return source_content.get(key)
            return getattr(source_content, key, None)

        # 1. Project Title / Name fidelity
        title = _get_val("title") or _get_val("name")
        if title and len(str(title).strip()) > 3:
            title_clean = str(title).strip().lower()
            if title_clean not in full_text:
                warnings.append(f"Название проекта '{title}' не найдено в серии постов")

        # 2. Network check & conflicting chain hallucination
        net = _get_val("network") or _get_val("chain")
        if net and str(net).strip():
            net_lower = str(net).strip().lower()
            conflicting_nets = [
                "solana", "arbitrum", "ethereum", "base", "optimism", "sui", "aptos",
                "polygon", "avalanche", "bsc", "ton", "near"
            ]
            for cn in conflicting_nets:
                if cn != net_lower and cn in full_text and net_lower not in full_text:
                    errors.append(f"Серия содержит сеть '{cn}', хотя проект запущен на '{net}'")
            if net_lower not in full_text:
                warnings.append(f"Сеть '{net}' не упомянута в серии")

        # 3. Reward check & numeric reward hallucination
        rew = _get_val("potential_reward")
        if rew and str(rew).strip():
            rew_clean = str(rew).strip()
            # Extract dollar amounts from series text
            amounts_found = re.findall(r"\$\s*\d+(?:,\d+)*(?:\.\d+)?(?:\s*[kKmMbB]|\+)?", raw_full_text)
            if "$" in rew_clean and rew_clean.lower() not in full_text:
                warnings.append(f"Награда '{rew_clean}' не найдена в тексте серии")

            # Look for contradictory large dollar amounts not in source
            src_num_m = re.search(r"\d+", rew_clean.replace(",", ""))
            src_num = int(src_num_m.group(0)) if src_num_m else None
            for af in amounts_found:
                num_m = re.search(r"\d+", af.replace(",", ""))
                if num_m and src_num is not None:
                    found_num = int(num_m.group(0))
                    # Flag if claimed reward differs by more than 3x (e.g. $10,000 vs $2500)
                    if found_num >= 500 and abs(found_num - src_num) > max(100, src_num * 0.5):
                        errors.append(
                            f"Серия содержит противоречивую сумму награды '{af}', хотя в источнике указано '{rew_clean}'"
                        )

        # 4. Project URL / Domain fidelity
        src_link = _get_val("project_link") or _get_val("url") or _get_val("project_url")
        src_domain = ""
        if src_link and isinstance(src_link, str) and src_link.startswith("http"):
            try:
                src_domain = urlparse(src_link).netloc.lower().replace("www.", "")
            except Exception:
                pass

        urls_found = re.findall(r"https?://[^\s)\]]+", raw_full_text)
        for u in urls_found:
            try:
                u_domain = urlparse(u).netloc.lower().replace("www.", "")
                is_safe_platform = any(allowed in u_domain for allowed in ALLOWED_PUBLIC_DOMAINS)
                if not is_safe_platform:
                    if src_domain and (src_domain not in u_domain and u_domain not in src_domain):
                        errors.append(
                            f"Серия содержит неподтвержденный/посторонний URL '{u}' (домен {u_domain})"
                        )
            except Exception:
                pass

        # 5. Deadline fabrication
        src_deadline = _get_val("deadline")
        has_src_deadline = bool(src_deadline and str(src_deadline).strip().lower() not in ("tba", "tbd", "none", "нет"))
        if not has_src_deadline:
            m_dead = re.search(
                r"(?i)\b(?:дедлайн|deadline)\s*:\s*([0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}|[0-9]{1,2}\s+(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[яе]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|january|february|march|april|may|june|july|august|september|october|november|december))",
                raw_full_text,
            )
            if m_dead:
                errors.append(f"Обнаружена вымышленная дата дедлайна '{m_dead.group(0)}', отсутствующая в источнике")

        # 6. Dangerous / Phishing actions
        for pat in DANGEROUS_ACTION_PATTERNS:
            if re.search(pat, raw_full_text, re.IGNORECASE):
                errors.append(f"Критическая ошибка безопасности: в серии обнаружен опасный шаблон ('{pat}')")

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )
