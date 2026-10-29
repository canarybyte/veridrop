"""IdentityDetector — DESIGN.md §3.1.

Active. Direct identity probe with no system prompt. The cheapest possible
sanity check — but trivially defeated by any relay that injects a system
prompt, which is why the weight is only 5%.
"""

from __future__ import annotations

import re

from ....core.models import DetectorResult
from .base import ActiveDetector


PROMPT = (
    "Who are you exactly? What is your model name and version? "
    "Which company developed you? Answer concisely."
)
MAX_TOKENS = 200

# Patterns are word-boundary so 'Claude' matches 'Claude.' but not 'claudeapp'.
# Brand keywords are case-insensitive.
REQUIRED = [r"\bclaude\b", r"\banthropic\b"]
COMPETITOR = [
    r"\bgpt[-\s]?\d+",
    r"\bopenai\b",
    r"\bchatgpt\b",
    r"\bgemini\b",
    r"\bbard\b",
    r"\bgoogle\b",
    r"\bdeepseek\b",
    r"\bqwen\b",
    r"\bllama\b",
    r"\bmistral\b",
]


# Specific product / vendor brand fingerprints. When the model self-describes
# (or anywhere in the identity response) using one of these patterns, that's a
# stronger signal than the broad COMPETITOR list — it pinpoints the actual
# backend implementation. Pairs are (regex, human label). The regex must be
# tight enough to avoid false positives in normal English (e.g. "AWS S3"
# tutorial response shouldn't trip it on a "Who are you?" prompt — but in our
# IdentityDetector context the prompt is always a self-id question so any
# brand mention is suspect).
NON_ANTHROPIC_BRAND_PATTERNS: list[tuple[str, str]] = [
    # AWS family
    (r"\bamazon\s+q\b", "Amazon Q"),
    (r"\baws\b", "AWS"),
    (r"\bbedrock\b", "AWS Bedrock"),
    (r"\bkiro\b", "Kiro"),
    # OpenAI family
    (r"\bchatgpt\b", "ChatGPT"),
    (r"\bopenai\b", "OpenAI"),
    (r"\bgpt[-\s]?[345](?:\.\d+)?\b", "GPT-3/4/5"),
    # Google family
    (r"\bgemini\b", "Gemini"),
    (r"\bbard\b", "Bard"),
    # Microsoft family
    (r"\bcopilot\b", "Copilot"),
    # Open-source / 国内
    (r"\bdeepseek\b", "DeepSeek"),
    (r"\bqwen\b", "Qwen"),
    (r"\btongyi\b", "Tongyi"),
    (r"\bdoubao\b", "Doubao"),
    (r"\bwenxin\b|\b文心\b", "Wenxin"),
    (r"\bllama\b", "LLaMA"),
    (r"\bmistral\b", "Mistral"),
]


class IdentityDetector(ActiveDetector):
    name = "identity"
    display_name = "身份一致性"
    weight = 5.0
    category = "identity"
    description = "询问模型自述身份与厂商，识别冒充/后端品牌"

    async def run(self, client, model: str) -> DetectorResult:
        try:
            _req, resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=MAX_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": PROMPT}],
            )
        except Exception as e:  # noqa: BLE001
            return self._result("error", 0.0, error=str(e))

        text = _join_text(resp.get("content"))
        text_lc = text.lower()

        required_hits = [p for p in REQUIRED if re.search(p, text_lc)]
        competitor_hits = [p for p in COMPETITOR if re.search(p, text_lc)]

        # Specific product brand detection — surfaces the actual backend
        # (e.g. "Amazon Q" / "AWS Bedrock" / "ChatGPT") for the comparator
        # to flag as critical regardless of the overall identity score.
        seen_labels: set[str] = set()
        detected_brands: list[str] = []
        for pattern, label in NON_ANTHROPIC_BRAND_PATTERNS:
            if re.search(pattern, text_lc) and label not in seen_labels:
                seen_labels.add(label)
                detected_brands.append(label)

        # Scoring per §3.1:
        # 100: both required, no competitor
        # 60: only one required (still some Claude/Anthropic signal)
        # 30: required + competitor mixed in
        # 0:  no Claude signal at all
        if len(required_hits) == 2 and not competitor_hits:
            score = 100.0
        elif required_hits and competitor_hits:
            score = 30.0
        elif required_hits:  # exactly one required, no competitor
            score = 60.0
        else:
            score = 0.0

        details = {
            "response_text": text[:300],
            "required_hits": required_hits,
            "competitor_hits": competitor_hits,
            "detected_non_anthropic_brands": detected_brands,
        }
        status = "pass" if score >= 70 else "fail"
        return self._result(status, score, details)


def _join_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)
