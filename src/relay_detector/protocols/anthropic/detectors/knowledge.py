"""KnowledgeDetector — DESIGN.md §3.5.

Active. Two-mode question delivery (DESIGN §6.4):
  - critical questions: sent in their own request, one per call. Used for
    cutoff-sensitive probes that must not get glossed over in a long context.
  - coverage questions: bundled into a single prompt, one numbered line per
    answer. Saves N-1 requests.

Per-model expected_by_model overrides the global expected_keywords when present.
"""

from __future__ import annotations

import importlib.resources as resources
import json
import re

from ....core.models import DetectorResult
from .base import ActiveDetector


MAX_TOKENS_CRITICAL = 60
MAX_TOKENS_COVERAGE = 400


def _load_questions() -> list[dict]:
    raw = (
        resources.files("relay_detector.protocols.anthropic.data")
        .joinpath("knowledge_questions.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw).get("questions", [])


class KnowledgeDetector(ActiveDetector):
    name = "knowledge"
    display_name = "知识准确度"
    weight = 10.0
    category = "capability"
    description = "用知识题验证真实能力与知识截止"

    async def run(self, client, model: str) -> DetectorResult:
        questions = _load_questions()
        applicable = [q for q in questions if _applies(q, model)]
        if not applicable:
            return self._result(
                "skip", 0.0, {"reason": "no applicable questions for model"}
            )

        critical = [q for q in applicable if q.get("type") == "critical"]
        coverage = [q for q in applicable if q.get("type") == "coverage"]

        per_question: list[dict] = []

        # --- critical: one request per question -----------------------
        for q in critical:
            try:
                _req, resp, _h, _lat = await client.messages_create(
                    model=model,
                    max_tokens=MAX_TOKENS_CRITICAL,
                    temperature=0,
                    messages=[{"role": "user", "content": q["prompt"]}],
                )
            except Exception as e:  # noqa: BLE001
                per_question.append(
                    {"id": q["id"], "passed": False, "error": str(e), "answer": ""}
                )
                continue
            answer = _join_text(resp.get("content"))
            ok = _grade(answer, q, model)
            per_question.append(
                {"id": q["id"], "passed": ok, "answer": answer[:200]}
            )

        # --- coverage: one combined request --------------------------
        if coverage:
            prompt = _build_combined_prompt(coverage)
            try:
                _req, resp, _h, _lat = await client.messages_create(
                    model=model,
                    max_tokens=MAX_TOKENS_COVERAGE,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                )
                answer_text = _join_text(resp.get("content"))
            except Exception as e:  # noqa: BLE001
                # Whole batch fails; mark each as failed with shared error
                for q in coverage:
                    per_question.append(
                        {"id": q["id"], "passed": False, "error": str(e), "answer": ""}
                    )
            else:
                parsed = _parse_numbered_answers(answer_text, len(coverage))
                for i, q in enumerate(coverage, start=1):
                    a = parsed.get(i, "")
                    ok = _grade(a, q, model)
                    per_question.append(
                        {"id": q["id"], "passed": ok, "answer": a[:200]}
                    )

        passes = sum(1 for r in per_question if r.get("passed"))
        total = len(per_question)
        score = (passes / total * 100.0) if total else 0.0
        status = "pass" if score >= 70 else "fail"
        return self._result(
            status,
            score,
            {
                "passes": passes,
                "total": total,
                "per_question": per_question,
                "request_count": len(critical) + (1 if coverage else 0),
            },
        )


def _applies(q: dict, model: str) -> bool:
    allowlist = q.get("applicable_models")
    if not allowlist:
        return True
    return any(model.startswith(m) or m.startswith(model) for m in allowlist)


def _build_combined_prompt(coverage: list[dict]) -> str:
    lines = [
        f"Please answer these {len(coverage)} questions briefly. "
        "Reply with one short answer per line, prefixed by question number "
        "(e.g. '1. <answer>'). If you don't know an answer, reply 'unknown' "
        "for that line — do not guess."
    ]
    for i, q in enumerate(coverage, start=1):
        lines.append(f"{i}. {q['prompt']}")
    return "\n\n".join(lines)


# Match "1. answer", "2) answer", "3: answer", "  4 - answer", etc.
_LINE_RE = re.compile(r"^\s*(\d+)\s*[\.\):\-]\s*(.+?)\s*$")


def _parse_numbered_answers(text: str, n: int) -> dict[int, str]:
    out: dict[int, str] = {}
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        if 1 <= idx <= n:
            out[idx] = m.group(2)
    return out


def _grade(answer: str, q: dict, model: str) -> bool:
    """Return True if the answer hits expected keywords AND avoids anti keywords."""
    if not answer:
        return False
    a = answer.lower()
    anti = [k.lower() for k in q.get("anti_keywords") or []]
    if any(k in a for k in anti):
        return False
    expected_by_model = q.get("expected_by_model") or {}
    expected = None
    for m_key, kws in expected_by_model.items():
        if model.startswith(m_key) or m_key.startswith(model):
            expected = [k.lower() for k in kws]
            break
    if expected is None:
        expected = [k.lower() for k in q.get("expected_keywords") or []]
    if not expected:
        return True  # nothing to check
    match_mode = q.get("expected_keyword_match", "all")
    if match_mode == "any":
        return any(k in a for k in expected)
    return all(k in a for k in expected)


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
