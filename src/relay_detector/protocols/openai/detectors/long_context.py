"""OpenAI long-context truncation detector — needle-in-haystack.

Tests whether the relay actually delivers the model's advertised context
window or silently truncates / routes to a smaller-window model.

Strategy (tiered, stop-on-first-failure):

  Tier 1: 32k tokens   — catches transport-layer truncation (nginx body cap)
  Tier 2: 100k tokens  — catches mid-tier truncation (relay summarizes long inputs)
  Tier 3: 200k tokens  — catches high-end truncation (relay routes 4o → 4o-mini)

Each tier sends a haystack with three needles at 10% / 50% / 90% positions
and asks the model to retrieve them. We stop probing further tiers as
soon as one fails — there's no point spending $0.30 on the 200k probe
when the 32k probe already proved truncation at 16k.

Opt-in (config.include_long_context). Default: skipped.
"""

from __future__ import annotations

import asyncio
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

# Each tier's pass threshold. Real models occasionally miss a needle —
# Anthropic and OpenAI both publish ~99% recall, so 2/3 needles is "minor
# wobble", 1/3 is "definite truncation", 0/3 is "completely cut off".
PASS_THRESHOLD = 3      # all needles → pass
PARTIAL_THRESHOLD = 2   # 2/3 → partial (warn but continue)
FAIL_THRESHOLD = 1      # ≤1 → fail (stop probing higher tiers)

# Headroom for the response so the model can recite three IDs comfortably.
# Reasoning models (o1/o3/gpt-5) burn extra reasoning tokens, so leave a
# generous buffer or finish_reason=length cuts off the answer mid-list.
MAX_OUTPUT_TOKENS = 256


def _tier_timeout_s(target_tokens: int) -> float:
    """Per-tier HTTP timeout. The default 30s client timeout is too short
    for big probes — empirically 1M tokens of input take 2–4 minutes
    upstream depending on model load. Scale with input size, with a 120s
    floor to handle network jitter on small probes."""
    return max(120.0, target_tokens / 4_000.0)


# Substring markers that indicate the upstream rejected our request for
# rate-limit reasons rather than because it truncated the input. Cover
# OpenAI's "tokens per min" / "rate_limit_exceeded" wording and the
# generic "HTTP 429" prefix our client raises.
_RATE_LIMIT_MARKERS = (
    "http 429",
    "rate limit",
    "rate_limit_exceeded",
    "tokens per min",
    "tpm",
    "requests per min",
)


def _looks_rate_limited(err_msg: str) -> bool:
    """Conservative rate-limit detection — false positives here would
    let a real truncation slip through as "rate_limited", so prefer
    explicit TPM / 429 markers over generic 'limit' text."""
    if not err_msg:
        return False
    lower = err_msg.lower()
    return any(m in lower for m in _RATE_LIMIT_MARKERS)


class LongContextDetector(ActiveDetector):
    name = "long_context"
    display_name = "长上下文真实性"
    weight = 15.0  # heavy when it runs — context window is a top-tier promise
    category = "capability"
    description = "验证长上下文窗口是否真实"

    async def run(self, client, model: str) -> DetectorResult:
        # Opt-in gate. `include_long_context_extreme` is the superset
        # (uses adaptive tiers up to model's advertised limit), so it
        # implies the standard one — checking either is enough to enable.
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
        # Extreme strategy: adaptive tiers up to ctx_limit. Standard:
        # hardcoded (32k, 100k, 200k). Extreme wins when both are checked.
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
                    "prompt_tokens_reported": None,
                })
                continue

            tier_result = await self._probe_tier_with_tpm_retry(
                client, model, target_tokens, seed, ctx_limit
            )
            tier_results.append(tier_result)
            total_cost_usd += tier_result["estimated_cost_usd"]
            reached_tier = target_tokens
            # Stop on rate_limited only after the TPM retry already
            # tried — at that point we've waited the full sliding-window
            # reset and still hit the wall, meaning user's tier quota
            # genuinely can't fit this probe size.
            if tier_result["status"] == "rate_limited":
                break
            if tier_result["status"] == "fail":
                # Strong truncation evidence (0-1/3 needles). Infer where
                # the relay caps. Partial tiers (2/3) are NOT truncation
                # evidence — both Claude and GPT have known lost-in-the-middle
                # behavior at advertised-limit tiers.
                last_pass = next(
                    (
                        t["target_tokens"]
                        for t in reversed(tier_results[:-1])
                        if t["status"] == "pass"
                    ),
                    None,
                )
                if last_pass is None:
                    truncation_at = target_tokens // 2  # rough lower-bound
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

    async def _probe_tier_with_tpm_retry(
        self,
        client,
        model: str,
        target_tokens: int,
        seed: str,
        ctx_limit: int,
    ) -> dict:
        """Run a tier; on rate_limited, sleep ~75s and retry once.

        ThrottledClient's built-in retry caps Retry-After at MAX_BACKOFF_S
        = 30s (fine for transient 429s, too short for OpenAI's 60s sliding
        TPM window). For long-context probes a single 524k probe leaves the
        next tier blocked for nearly a full minute — sleeping the full
        window plus a 15s safety margin is what unblocks 1M-tier tests.

        We only retry rate_limited results (truncation 'fail' isn't
        retryable — second attempt at the same input would hit the same
        truncation). Cap at one retry to keep the run bounded.
        """
        result = await self._probe_tier(
            client, model, target_tokens, seed, ctx_limit
        )
        if result["status"] != "rate_limited":
            return result

        # Sleep for the TPM window to fully reset, then re-probe once.
        wait_s = 75.0
        await asyncio.sleep(wait_s)
        retry = await self._probe_tier(
            client, model, target_tokens, seed, ctx_limit
        )
        retry["tpm_retry_attempted"] = True
        retry["tpm_retry_wait_s"] = wait_s
        # Preserve cost from BOTH attempts (rate-limited rejections
        # cost $0, but the successful retry costs the full tier price).
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
        """Run one tier: build haystack, send, score recall.

        Haystack is sized to leave ~QUESTION_BUFFER tokens of headroom for the
        question itself + a small response. For tiers at the model's exact
        context limit (e.g. 200k tier on a 200k model), the haystack is
        clamped to ctx_limit - buffer so the request actually fits.
        """
        QUESTION_BUFFER = 1500
        tier_seed = f"{seed}:{target_tokens}"
        needles = make_needles(tier_seed)
        haystack_target = min(
            target_tokens - QUESTION_BUFFER,
            ctx_limit - QUESTION_BUFFER,
        )
        haystack = assemble_haystack(
            haystack_target, needles, tier_seed, protocol="openai",
        )
        question = build_question(needles)
        full_prompt = haystack + question

        cost = estimate_cost_usd(target_tokens, model)
        timeout = _tier_timeout_s(target_tokens)
        try:
            _req, resp, _h, _lat = await client.chat_completions_create(
                model=model,
                max_completion_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": full_prompt}],
                request_timeout_s=timeout,
            )
        except Exception as e:  # noqa: BLE001
            # OpenAIAPIError truncates body to 200 chars in its str(),
            # which crops the diagnostic info we most need ("Requested
            # X tokens, please try again in Yms"). Pull the raw body
            # directly when it's an API error.
            body = getattr(e, "body", None)
            err_msg = (
                f"HTTP {getattr(e, 'status', '?')}: {body}"
                if isinstance(body, str) else str(e)
            )
            # 429 + TPM hits are NOT truncation evidence — they're the
            # provider's own rate limit on long-context tiers (OpenAI
            # exposes a separate `gpt-4.1-mini-long-context` SKU with its
            # own TPM cap). Marking them as fail would falsely accuse a
            # compliant relay of truncating. Treat as inconclusive.
            if _looks_rate_limited(err_msg):
                return {
                    "target_tokens": target_tokens,
                    "needles_total": len(needles),
                    "needles_found": 0,
                    "status": "rate_limited",
                    "error": err_msg[:1500],
                    "estimated_cost_usd": 0.0,
                    "prompt_tokens_reported": None,
                    "response_text_preview": None,
                }
            # Genuine transport-level failures (413 / timeout / 5xx /
            # context too long) — these ARE truncation evidence.
            return {
                "target_tokens": target_tokens,
                "needles_total": len(needles),
                "needles_found": 0,
                "status": "fail",
                "error": err_msg[:1500],
                "estimated_cost_usd": 0.0,
                "prompt_tokens_reported": None,
                "response_text_preview": None,
            }

        text = _message_text(resp)
        recalls = evaluate_recalls(text, needles)
        found = sum(recalls)
        usage = resp.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")

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
            "prompt_tokens_reported": prompt_tokens,
            "response_text_preview": text[:400],
        }


def _aggregate(tier_results: list[dict]) -> tuple[float, str, str]:
    """Combine per-tier outcomes into one score / status / human summary.

    Tier entry kinds:
      - pass/partial/fail: probed with a real request
      - skip:         model's own context limit lower than this tier
      - rate_limited: provider's TPM cap rejected request — NOT truncation
      - (absent):     loop broke after earlier fail/partial/rate_limited

    Scoring drops skip AND rate_limited entries — neither reflects a
    relay flaw. Penalizing rate_limited tiers would falsely accuse a
    compliant relay of truncating just because the user's API key has
    a low TPM ceiling on the long-context SKU.
    """
    if not tier_results:
        return 0.0, "error", "未跑任何 tier"

    inconclusive = {"skip", "rate_limited"}
    probed = [t for t in tier_results if t["status"] not in inconclusive]
    rate_limited = [t for t in tier_results if t["status"] == "rate_limited"]

    if not probed:
        # All tiers either over model limit or rate-limited. Distinguish
        # so the user knows which problem to fix.
        if rate_limited:
            t = rate_limited[0]
            return 0.0, "skip", (
                f"{t['target_tokens'] // 1000}k tokens probe 触发上游 "
                "rate limit (TPM/RPM),非中转站缺陷 —— 请稍后重试或换更高 tier 的 key"
            )
        return 0.0, "skip", "模型自身 context 上限低于检测最低档 (32k),跳过"

    per_tier_pct = []
    for t in probed:
        if t["status"] == "pass":
            per_tier_pct.append(100.0)
        elif t["status"] == "partial":
            per_tier_pct.append(66.0)
        else:  # fail
            per_tier_pct.append(0.0)
    score = sum(per_tier_pct) / len(per_tier_pct)

    has_fail = any(t["status"] == "fail" for t in probed)
    has_partial = any(t["status"] == "partial" for t in probed)
    skip_count = sum(1 for t in tier_results if t["status"] == "skip")

    if has_fail:
        bad = next(t for t in probed if t["status"] == "fail")
        status = "fail"
        summary = (
            f"{bad['target_tokens'] // 1000}k tokens 处召回失败 "
            f"({bad['needles_found']}/{bad['needles_total']} needles) "
            "—— 中转站很可能在此规模截断或路由到小窗口模型"
        )
    else:
        # has_partial possible here. Treat as pass-with-warning: the score
        # already reflects 2/3 (66 vs 100 per tier), and lost-in-the-middle
        # at advertised-limit tiers is observed even against ground-truth
        # APIs. Hard-failing on it makes the detector reject real models.
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


def _message_text(resp: dict) -> str:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""
