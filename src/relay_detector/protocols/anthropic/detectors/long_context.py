"""Anthropic long-context truncation detector — needle-in-haystack.

Mirrors the OpenAI implementation but speaks Anthropic Messages API:
  - client.messages_create(...) instead of chat_completions_create
  - max_tokens (not max_completion_tokens)
  - response uses content[].text blocks
  - usage.input_tokens (not prompt_tokens)

1M models are probed only when include_long_context_extreme is enabled; the
near-limit tier must be verified with count_tokens before sending.

Opt-in (config.include_long_context). Default: skipped.
"""

from __future__ import annotations

import asyncio
import re
import time

from ....core.long_context import (
    STANDARD_TIERS,
    assemble_haystack,
    build_question,
    estimate_cost_usd,
    evaluate_recalls,
    make_needles,
    model_context_limit,
    tiers_for_model,
)
from ....core.models import DetectorResult
from .base import ActiveDetector

PASS_THRESHOLD = 3
PARTIAL_THRESHOLD = 2

# Anthropic's max_tokens caps the OUTPUT, not the input. 256 is enough for
# the model to recite three IDs comfortably; some Anthropic models burn
# extra tokens on adaptive thinking, so leave headroom.
MAX_OUTPUT_TOKENS = 256
QUESTION_BUFFER = 1500
TOKEN_COUNT_MARGIN = 500
MAX_TOKEN_COUNT_ATTEMPTS = 2
NEAR_LIMIT_PRECOUNT_THRESHOLD = 0.80
NEAR_LIMIT_INITIAL_TARGET_RATIO = 0.62
TOKEN_TARGET_TOLERANCE_FRAC = 0.02

_PROMPT_TOO_LONG_RE = re.compile(
    r"prompt is too long:\s*([\d,]+)\s*tokens?\s*>\s*([\d,]+)\s*maximum",
    re.IGNORECASE,
)


def _tier_timeout_s(target_tokens: int) -> float:
    """Per-tier HTTP timeout — see OpenAI variant for full rationale.
    Same scaling: 120s floor, +1s per 4k tokens. 950k → ~240s."""
    return max(120.0, target_tokens / 4_000.0)


_RATE_LIMIT_MARKERS = (
    "http 429",
    "rate limit",
    "rate_limit_exceeded",
    "tokens per min",
    "tpm",
    "requests per min",
    "overloaded_error",  # Anthropic's transient overload signal
)


def _looks_rate_limited(err_msg: str) -> bool:
    """See OpenAI long_context variant — same logic, same markers plus
    Anthropic's `overloaded_error` which is also transient and not a
    truncation signal."""
    if not err_msg:
        return False
    lower = err_msg.lower()
    return any(m in lower for m in _RATE_LIMIT_MARKERS)


def _requires_precise_count(target_tokens: int, ctx_limit: int) -> bool:
    return (
        ctx_limit >= 1_000_000
        and target_tokens >= int(ctx_limit * NEAR_LIMIT_PRECOUNT_THRESHOLD)
    )


def _initial_haystack_target(target_tokens: int, ctx_limit: int) -> int:
    target = min(
        target_tokens - QUESTION_BUFFER,
        ctx_limit - QUESTION_BUFFER,
    )
    if _requires_precise_count(target_tokens, ctx_limit):
        # Opus 1M synthetic haystacks have been observed to tokenize far
        # denser than the shared Anthropic estimate: a nominal 950k prompt
        # can count as ~1.56M tokens. Start below the nominal tier and let
        # count_tokens tighten the final size.
        target = int(target * NEAR_LIMIT_INITIAL_TARGET_RATIO)
    return max(1000, target)


def _within_token_target(
    counted_tokens: int, desired_tokens: int, count_budget: int
) -> bool:
    lower = int(desired_tokens * (1.0 - TOKEN_TARGET_TOLERANCE_FRAC))
    upper = min(
        count_budget,
        int(desired_tokens * (1.0 + TOKEN_TARGET_TOLERANCE_FRAC)),
    )
    return lower <= counted_tokens <= upper


def _looks_detector_prompt_overflow(err_msg: str, ctx_limit: int) -> bool:
    m = _PROMPT_TOO_LONG_RE.search(err_msg or "")
    if not m:
        return False
    requested = int(m.group(1).replace(",", ""))
    maximum = int(m.group(2).replace(",", ""))
    return requested > maximum and maximum == ctx_limit


def _skip_tier(
    target_tokens: int,
    needles_total: int,
    reason: str,
    *,
    error: str | None = None,
    input_tokens_precounted: int | None = None,
    count_tokens_attempts: int = 0,
    sizing_iterations: int = 0,
) -> dict:
    result = {
        "target_tokens": target_tokens,
        "needles_total": needles_total,
        "needles_found": 0,
        "status": "skip",
        "skip_reason": reason,
        "estimated_cost_usd": 0.0,
        "input_tokens_reported": None,
        "input_tokens_precounted": input_tokens_precounted,
        "count_tokens_attempts": count_tokens_attempts,
        "sizing_iterations": sizing_iterations,
        "response_text_preview": None,
    }
    if error:
        result["error"] = error[:1500]
    return result


class LongContextDetector(ActiveDetector):
    name = "long_context"
    display_name = "长上下文真实性"
    weight = 15.0  # heavy — context-window fraud is among the worst lies

    async def run(self, client, model: str) -> DetectorResult:
        # Opt-in gate. include_long_context_extreme is the superset (uses
        # adaptive tiers up to model's advertised limit), so it implies
        # the standard one — checking either is enough to enable.
        cfg = self.config
        opt_in_standard = bool(cfg and cfg.include_long_context)
        opt_in_extreme = bool(cfg and cfg.include_long_context_extreme)
        if not (opt_in_standard or opt_in_extreme):
            return self.skip(
                "长上下文检测为可选项,需在请求时勾选(标准档 $0.05–$0.50 / 极限档 $0.05–$8)"
            )

        seed = f"{client.base_url}:{model}:{int(time.time())}"
        tier_results: list[dict] = []
        total_cost_usd = 0.0
        truncation_at: int | None = None
        reached_tier: int | None = None

        ctx_limit = model_context_limit(model)
        # Extreme strategy: adaptive tiers up to ctx_limit (e.g. 32k →
        # 500k → 950k for a 1M model). Standard: hardcoded (32k, 100k,
        # 200k). Extreme wins when both are checked.
        if opt_in_extreme:
            tier_set = tiers_for_model(ctx_limit)
            tier_strategy = "extreme"
        else:
            tier_set = STANDARD_TIERS
            tier_strategy = "standard"

        for target_tokens in tier_set:
            if target_tokens > ctx_limit:
                tier_results.append({
                    "target_tokens": target_tokens,
                    "needles_total": 3,
                    "needles_found": 0,
                    "status": "skip",
                    "skip_reason": (
                        f"模型 {model} 上限为 {ctx_limit} tokens,跳过此档"
                    ),
                    "estimated_cost_usd": 0.0,
                    "input_tokens_reported": None,
                })
                continue

            tier_result = await self._probe_tier_with_tpm_retry(
                client, model, target_tokens, seed, ctx_limit
            )
            tier_results.append(tier_result)
            total_cost_usd += tier_result["estimated_cost_usd"]
            reached_tier = target_tokens
            # Stop on rate_limited too — TPM windows reset on the order
            # of minutes, retrying the next tier within this run hits
            # the same wall.
            if tier_result["status"] == "rate_limited":
                break
            if tier_result["status"] == "fail":
                # Strong truncation evidence (0-1/3 needles found) — infer
                # where the relay caps. Partial tiers (2/3) are NOT
                # truncation evidence: Claude's known lost-in-the-middle
                # behavior at the 200k tier ceiling can produce 2/3 even
                # against api.anthropic.com directly.
                last_pass = next(
                    (
                        t["target_tokens"]
                        for t in reversed(tier_results[:-1])
                        if t["status"] == "pass"
                    ),
                    None,
                )
                if last_pass is None:
                    truncation_at = target_tokens // 2
                else:
                    truncation_at = (last_pass + target_tokens) // 2
                break
            if tier_result["status"] == "partial":
                # Stop probing higher tiers (likely same wobble), but
                # don't claim truncation.
                break

        score, status, summary = _aggregate(tier_results)

        return self._result(
            status,
            score,
            {
                "summary": summary,
                "tier_strategy": tier_strategy,
                "tiers_tested": tier_results,
                "highest_tier_reached": reached_tier,
                "truncation_inferred_at_tokens": truncation_at,
                "estimated_cost_usd": round(total_cost_usd, 4),
                "model": model,
                "model_context_limit": ctx_limit,
                "opt_in": True,
            },
        )

    async def _precount_input_tokens(
        self, client, model: str, prompt: str
    ) -> int | None:
        """Ask Anthropic exactly how many input_tokens the request will be,
        without sending it.

        Returns None on any failure (relay doesn't implement the endpoint,
        rate-limited, network error). Low-risk tiers may still fall back to
        the chars/token estimate; near-limit or already-trimmed tiers skip
        rather than risk a false truncation verdict.
        """
        try:
            _req, resp, _h, _lat = await client.count_tokens(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:  # noqa: BLE001 — best-effort verification only
            return None
        n = resp.get("input_tokens") if isinstance(resp, dict) else None
        return n if isinstance(n, int) else None

    async def _probe_tier_with_tpm_retry(
        self,
        client,
        model: str,
        target_tokens: int,
        seed: str,
        ctx_limit: int,
    ) -> dict:
        """Sleep ~75s and retry once on rate_limited — see OpenAI variant
        for full rationale. Anthropic's RPM window is also 60s sliding,
        so the same wait-out strategy applies."""
        result = await self._probe_tier(
            client, model, target_tokens, seed, ctx_limit
        )
        if result["status"] != "rate_limited":
            return result

        wait_s = 75.0
        await asyncio.sleep(wait_s)
        retry = await self._probe_tier(
            client, model, target_tokens, seed, ctx_limit
        )
        retry["tpm_retry_attempted"] = True
        retry["tpm_retry_wait_s"] = wait_s
        retry["estimated_cost_usd"] += result.get("estimated_cost_usd", 0.0)
        return retry

    async def _probe_tier(
        self,
        client,
        model: str,
        target_tokens: int,
        seed: str,
        ctx_limit: int,
    ) -> dict:
        # Use chars/tok estimation only as the FIRST guess. The real source
        # of truth is Anthropic's /v1/messages/count_tokens endpoint: trim
        # against the counted size and re-count before sending.
        tier_seed = f"{seed}:{target_tokens}"
        needles = make_needles(tier_seed)
        haystack_target = _initial_haystack_target(target_tokens, ctx_limit)
        haystack = assemble_haystack(
            haystack_target, needles, tier_seed, protocol="anthropic",
        )
        question = build_question(needles)
        full_prompt = haystack + question

        count_budget = ctx_limit - TOKEN_COUNT_MARGIN
        desired_count = min(target_tokens, count_budget)
        count_required = _requires_precise_count(target_tokens, ctx_limit)
        count_tokens_attempts = 0
        sizing_iterations = 0
        precounted: int | None = None

        for attempt in range(MAX_TOKEN_COUNT_ATTEMPTS):
            count_tokens_attempts += 1
            precounted = await self._precount_input_tokens(
                client, model, full_prompt
            )
            if precounted is None:
                if count_required or sizing_iterations:
                    return _skip_tier(
                        target_tokens,
                        len(needles),
                        (
                            "count_tokens unavailable for required Anthropic "
                            "long-context sizing; skipped to avoid a false "
                            "truncation verdict"
                        ),
                        count_tokens_attempts=count_tokens_attempts,
                        sizing_iterations=sizing_iterations,
                    )
                break
            if count_required:
                if _within_token_target(precounted, desired_count, count_budget):
                    break
            elif precounted <= count_budget:
                break
            if attempt == MAX_TOKEN_COUNT_ATTEMPTS - 1:
                return _skip_tier(
                    target_tokens,
                    len(needles),
                    (
                        "detector prompt could not be sized to the requested "
                        "token tier after count-driven adjustment"
                    ),
                    error=(
                        f"count_tokens={precounted}, desired={desired_count}, "
                        f"budget={count_budget}"
                    ),
                    input_tokens_precounted=precounted,
                    count_tokens_attempts=count_tokens_attempts,
                    sizing_iterations=sizing_iterations,
                )

            resize_ratio = desired_count / max(precounted, 1)
            # When shrinking, keep a small safety margin. When growing, aim
            # directly at the requested tier and verify again before sending.
            safety = 0.99 if resize_ratio < 1.0 else 1.0
            haystack_target = max(
                1000,
                int(haystack_target * resize_ratio * safety),
            )
            sizing_iterations += 1
            haystack = assemble_haystack(
                haystack_target, needles, tier_seed, protocol="anthropic",
            )
            full_prompt = haystack + question

        cost = estimate_cost_usd(target_tokens, model)
        timeout = _tier_timeout_s(target_tokens)
        try:
            _req, resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": full_prompt}],
                request_timeout_s=timeout,
            )
        except Exception as e:  # noqa: BLE001
            # Pull raw body when it's an AnthropicAPIError — its str() is
            # capped at 200 chars which crops important diagnostic detail
            # like "Requested X, please try again in Y".
            body = getattr(e, "body", None)
            err_msg = (
                f"HTTP {getattr(e, 'status', '?')}: {body}"
                if isinstance(body, str) else str(e)
            )
            # Distinguish rate-limit (provider's TPM/RPM cap, not
            # truncation) from real failures (413 / context-too-long /
            # timeout). The first should NOT count against the relay.
            if _looks_rate_limited(err_msg):
                return {
                    "target_tokens": target_tokens,
                    "needles_total": len(needles),
                    "needles_found": 0,
                    "status": "rate_limited",
                    "error": err_msg[:1500],
                    "estimated_cost_usd": 0.0,
                    "input_tokens_reported": None,
                    "response_text_preview": None,
                }
            if _looks_detector_prompt_overflow(err_msg, ctx_limit):
                return _skip_tier(
                    target_tokens,
                    len(needles),
                    (
                        "provider reported the constructed prompt exceeds "
                        "the known model context limit; treating as detector "
                        "prompt overflow, not relay truncation"
                    ),
                    error=err_msg,
                    input_tokens_precounted=precounted,
                    count_tokens_attempts=count_tokens_attempts,
                    sizing_iterations=sizing_iterations,
                )
            return {
                "target_tokens": target_tokens,
                "needles_total": len(needles),
                "needles_found": 0,
                "status": "fail",
                "error": err_msg[:1500],
                "estimated_cost_usd": 0.0,
                "input_tokens_reported": None,
                "response_text_preview": None,
            }

        text = _join_text(resp.get("content"))
        recalls = evaluate_recalls(text, needles)
        found = sum(recalls)
        usage = resp.get("usage") or {}
        input_tokens = usage.get("input_tokens")

        if found >= PASS_THRESHOLD:
            tier_status = "pass"
        elif found >= PARTIAL_THRESHOLD:
            tier_status = "partial"
        else:
            tier_status = "fail"

        return {
            "target_tokens": target_tokens,
            "needles_total": len(needles),
            "needles_found": found,
            "needle_recalls": [
                {"label": n.label, "found": r}
                for n, r in zip(needles, recalls)
            ],
            "status": tier_status,
            "estimated_cost_usd": cost,
            "input_tokens_reported": input_tokens,
            "input_tokens_precounted": precounted,
            "count_tokens_attempts": count_tokens_attempts,
            "sizing_iterations": sizing_iterations,
            "response_text_preview": text[:400],
        }


def _aggregate(tier_results: list[dict]) -> tuple[float, str, str]:
    """Same scoring philosophy as the OpenAI variant — drop skip tiers
    from the average so model-limit constraints don't penalize the relay.
    See protocols/openai/detectors/long_context.py:_aggregate for the full
    rationale."""
    if not tier_results:
        return 0.0, "error", "未跑任何 tier"

    inconclusive = {"skip", "rate_limited"}
    probed = [t for t in tier_results if t["status"] not in inconclusive]
    rate_limited = [t for t in tier_results if t["status"] == "rate_limited"]
    skipped = [t for t in tier_results if t["status"] == "skip"]

    if not probed:
        if rate_limited:
            t = rate_limited[0]
            return 0.0, "skip", (
                f"{t['target_tokens'] // 1000}k tokens probe 触发上游 "
                "rate limit (TPM/RPM),非中转站缺陷 —— 请稍后重试或换更高 tier 的 key"
            )
        if skipped:
            reason = skipped[0].get("skip_reason")
            if isinstance(reason, str) and reason:
                return 0.0, "skip", reason
        return 0.0, "skip", "模型自身 context 上限低于检测最低档 (32k),跳过"

    per_tier_pct = []
    for t in probed:
        if t["status"] == "pass":
            per_tier_pct.append(100.0)
        elif t["status"] == "partial":
            per_tier_pct.append(66.0)
        else:
            per_tier_pct.append(0.0)
    score = sum(per_tier_pct) / len(per_tier_pct)

    has_fail = any(t["status"] == "fail" for t in probed)
    has_partial = any(t["status"] == "partial" for t in probed)
    skip_count = sum(1 for t in tier_results if t["status"] == "skip")

    # has_fail = 0-1/3 needles = strong truncation signal → status=fail
    # has_partial = 2/3 needles, no fail = soft signal. Don't fail outright:
    #   Claude (and OpenAI) both have known lost-in-the-middle behavior at
    #   the limit of advertised context — a 50% needle missed at 200k tier
    #   on real claude-haiku-4-5 has been observed in our own live tests
    #   against api.anthropic.com (the truth source). Calling that "fail"
    #   makes the detector reject the ground-truth model. Score still
    #   reflects 2/3 (66%) so the summary surfaces the wobble, but the
    #   tier verdict is pass — the signal isn't strong enough for fail.
    if has_fail:
        bad = next(t for t in probed if t["status"] == "fail")
        status = "fail"
        summary = (
            f"{bad['target_tokens'] // 1000}k tokens 处召回失败 "
            f"({bad['needles_found']}/{bad['needles_total']} needles) "
            "—— 中转站很可能在此规模截断或路由到小窗口模型"
        )
    else:
        highest = probed[-1]["target_tokens"] // 1000
        status = "pass"
        suffix_parts = []
        if has_partial:
            partial = next(t for t in probed if t["status"] == "partial")
            suffix_parts.append(
                f"{partial['target_tokens'] // 1000}k 档召回 "
                f"{partial['needles_found']}/{partial['needles_total']}"
                "(模型在长上下文中段位置的自然召回缺失,非截断)"
            )
        if skip_count > 0:
            skipped_for_count = any(
                "count_tokens" in str(t.get("skip_reason", ""))
                or "prompt overflow" in str(t.get("skip_reason", ""))
                for t in skipped
            )
            if skipped_for_count:
                suffix_parts.append("更高档因 count_tokens/构造尺寸诊断未测")
            else:
                suffix_parts.append("更高档因模型自身上限未测")
        if rate_limited:
            rl = rate_limited[0]
            suffix_parts.append(
                f"{rl['target_tokens'] // 1000}k 档触发上游 TPM 限制(非截断,未计分)"
            )
        if suffix_parts:
            summary = f"完整通过 {highest}k tokens 长上下文检测;" + ";".join(suffix_parts)
        else:
            summary = f"完整通过 {highest}k tokens 长上下文检测,未发现截断证据"

    return score, status, summary


def _join_text(content) -> str:
    """Concat all text blocks from an Anthropic Messages content array."""
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)
