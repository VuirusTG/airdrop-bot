"""Strict Task Validator and Sanitizer for DraftContent tasks[].

Enforces all mandatory Task Rules:
- Length <= 120 characters
- No silent ellipsis ('...' or '…')
- No filler ('etc.', 'and more', 'and so on')
- No markdown formatting (**bold**, *italic*)
- No embedded numbering ('1.', 'Step 1:')
- No emojis inside task text
- No duplicates
- Between 1 and 5 tasks
"""
from __future__ import annotations

import re
from dataclasses import dataclass

FORBIDDEN_PHRASES_RE = re.compile(
    r"(\.\.\.|…|\betc\.?|\band\s+more\b|\band\s+so\s+on\b|\band\s+others\b)",
    re.IGNORECASE,
)
EMBEDDED_NUMBERING_RE = re.compile(
    r"^\s*(?:step\s*\d+[:.\s-]*|\d+[\.\)]\s*|[-*•]\s*)",
    re.IGNORECASE,
)
MARKDOWN_CHARS_RE = re.compile(r"[*_`~#\[\]]")
EMOJI_RE = re.compile(
    r"[\U00010000-\U0010ffff]|[\uD800-\uDBFF][\uDC00-\uDFFF]|"
    r"[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]|[\u203C\u2049\u2139\u2194-\u21AA]"
)


@dataclass
class ValidationIssue:
    task_index: int
    task_text: str
    reason: str


@dataclass
class ValidationResult:
    is_valid: bool
    issues: list[ValidationIssue]

    @property
    def error_summary(self) -> str:
        if not self.issues:
            return ""
        return "; ".join(f"Task #{iss.task_index + 1}: {iss.reason}" for iss in self.issues)


def sanitize_task(text: str) -> str:
    """Pre-cleaning pass to strip external formatting before strict validation."""
    cleaned = (text or "").strip()
    # Strip leading step numbers e.g. "1. ", "Step 1: ", "- "
    cleaned = EMBEDDED_NUMBERING_RE.sub("", cleaned).strip()
    # Strip markdown artifacts
    cleaned = MARKDOWN_CHARS_RE.sub("", cleaned)
    # Strip emojis from tasks
    cleaned = EMOJI_RE.sub("", cleaned)
    # Collapse multiple whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Strip trailing punctuation, but do not hide ellipsis if present
    if not cleaned.endswith("...") and not cleaned.endswith("…"):
        cleaned = cleaned.rstrip(" :;,-.")
    return cleaned


def validate_single_task(task: str, index: int = 0) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    t = (task or "").strip()

    if not t:
        issues.append(ValidationIssue(index, task, "Task cannot be empty"))
        return issues

    # Check forbidden ellipsis and filler
    match = FORBIDDEN_PHRASES_RE.search(t)
    if match:
        issues.append(
            ValidationIssue(
                index,
                task,
                f"Contains forbidden ellipsis or filler words: '{match.group(1)}'",
            )
        )

    if len(t) > 120:
        issues.append(
            ValidationIssue(
                index,
                task,
                f"Task exceeds 120 characters limit ({len(t)} chars)",
            )
        )

    # Check embedded step numbers that weren't sanitized
    if EMBEDDED_NUMBERING_RE.search(t):
        issues.append(
            ValidationIssue(
                index,
                task,
                "Contains embedded step numbering (numbering is rendered automatically)",
            )
        )

    # Check for markdown syntax
    if MARKDOWN_CHARS_RE.search(t):
        issues.append(
            ValidationIssue(
                index,
                task,
                "Contains Markdown syntax (Markdown inside tasks is forbidden)",
            )
        )

    # Check for emojis
    if EMOJI_RE.search(t):
        issues.append(
            ValidationIssue(
                index,
                task,
                "Contains emoji inside task text (emojis are added by the post layout)",
            )
        )

    return issues


def validate_tasks(tasks: list[str]) -> ValidationResult:
    """Deterministic validation of the entire task list."""
    issues: list[ValidationIssue] = []

    if not isinstance(tasks, list):
        return ValidationResult(
            is_valid=False,
            issues=[ValidationIssue(0, str(tasks), "Tasks must be an array of strings")],
        )

    if len(tasks) < 1:
        return ValidationResult(
            is_valid=False,
            issues=[ValidationIssue(0, "", "Task list must contain at least 1 task")],
        )

    if len(tasks) > 5:
        return ValidationResult(
            is_valid=False,
            issues=[
                ValidationIssue(
                    0,
                    f"Count: {len(tasks)}",
                    f"Task list exceeds maximum of 5 tasks ({len(tasks)} provided)",
                )
            ],
        )

    seen_tasks = set()
    for idx, raw_task in enumerate(tasks):
        if not isinstance(raw_task, str):
            issues.append(
                ValidationIssue(idx, str(raw_task), "Each task element must be a string")
            )
            continue

        # Check raw task first for strict rules
        item_issues = validate_single_task(raw_task, idx)
        issues.extend(item_issues)

        sanitized = sanitize_task(raw_task)
        normalized_key = sanitized.lower()
        if normalized_key in seen_tasks:
            issues.append(ValidationIssue(idx, raw_task, "Duplicate task found"))
        seen_tasks.add(normalized_key)

    return ValidationResult(is_valid=len(issues) == 0, issues=issues)
