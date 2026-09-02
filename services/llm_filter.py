"""Classify whether a signal is a relevant and reasonably safe opportunity."""
import json
from dataclasses import dataclass

from google.genai import types

from config import settings
from services.gemini_client import generate_content


SYSTEM_PROMPT = """You screen crypto signals for an airdrop/testnet editor.
The editor makes the final decision, so do not reject a relevant opportunity merely
because a short announcement lacks team, funding, or GitHub details. Distinguish:
1) relevance: is there a current action that could lead to rewards, points, access,
   eligibility, a testnet role, a quest, a waitlist, or a retroactive benefit?
2) safety/quality: how credible and safe is it?

Reject generic market news, price predictions, trading calls, educational articles,
old airdrop definitions, award announcements, and social chatter. Treat explicit
requests for seed phrases/private keys, guaranteed returns, or payment to unlock a
reward as critical risks. Normal testnet gas or optional onchain activity is not by
itself a critical risk, but must be mentioned.

Use a conservative score, but send uncertain relevant candidates to human review.
Respond with ONLY JSON:
{
  "is_opportunity": <boolean>,
  "has_current_action": <boolean>,
  "score": <float 0-10 for legitimacy/quality>,
  "confidence": "high" | "medium" | "low",
  "critical_risk": <boolean>,
  "verdict": "review" | "reject",
  "reasoning": "<2 concise sentences in Russian: relevance and risk>",
  "chain": "<ecosystem or null>",
  "category": "airdrop" | "testnet" | "quest" | "points" | "waitlist" | "other"
}"""


@dataclass
class FilterResult:
    score: float
    verdict: str
    reasoning: str
    chain: str | None
    category: str
    is_opportunity: bool
    has_current_action: bool
    confidence: str
    critical_risk: bool

    @property
    def passes(self) -> bool:
        return (
            self.is_opportunity
            and self.has_current_action
            and not self.critical_risk
            and self.score >= settings.FILTER_MIN_SCORE
        )


async def score_project(name: str, raw_text: str, source_url: str | None = None) -> FilterResult:
    user_content = f"Candidate: {name}\n"
    if source_url:
        user_content += f"Source URL: {source_url}\n"
    user_content += f"\nRaw signal:\n{raw_text[:7000]}"

    response = await generate_content(
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    data = json.loads(response.text)
    return FilterResult(
        score=float(data["score"]),
        verdict=data.get("verdict", "reject"),
        reasoning=data["reasoning"],
        chain=data.get("chain"),
        category=data.get("category", "other"),
        is_opportunity=bool(data.get("is_opportunity")),
        has_current_action=bool(data.get("has_current_action")),
        confidence=data.get("confidence", "low"),
        critical_risk=bool(data.get("critical_risk")),
    )
