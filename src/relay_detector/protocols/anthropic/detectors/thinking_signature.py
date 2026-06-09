"""ThinkingSignatureDetector — DESIGN.md §3.3 ⭐.

Active. The crown-jewel detector: extended/adaptive thinking emits a
signature_delta event whose payload is a server-side cryptographic signature.
A relay station impersonating Claude with another model literally cannot
forge this signature.

Sub-checks:
  100 — thinking block (or redacted_thinking) exists, signature non-empty
        and long enough to look like a real Anthropic signature
   70 — thinking block exists, signature present but suspiciously short
   30 — thinking block exists but signature_delta never arrived
    0 — thinking parameter ignored / no thinking block at all

applies_to() filters out models that don't support thinking at all.
For models that support both extended and adaptive (Sonnet 4.6), we prefer
extended because budget_tokens gives us tighter cost control.
"""

from __future__ import annotations

from ..config import lookup_model
from ....core.models import DetectorResult
from .base import ActiveDetector


# Multi-step Euclidean GCD: complex enough that Opus 4.7's adaptive thinking
# reliably decides to think (a simple multiplication wouldn't). Using
# different numbers from the official 1071/462 doc example so a relay
# can't special-case the canonical demo. We deliberately keep the prompt
# minimal — adding "walk through each step" hints at structure and can
# nudge the model away from thinking.
PROBE_PROMPT = (
    "Find the greatest common divisor of 2378 and 1547 using the Euclidean "
    "algorithm."
)
THINKING_BUDGET_TOKENS = 2000
# max_tokens caps thinking + response combined. With effort=high the adaptive
# model can use a lot of thinking tokens; setting this too low (e.g. 2600)
# causes the model to skip thinking entirely to fit. 16000 matches the
# official documentation examples.
MAX_TOKENS = 16000

# Empirically observed Anthropic thinking signatures are >> 100 chars; we
# settle for >= 50 as a generous lower bound.
SIGNATURE_MIN_LEN = 50


def _adaptive_effort_for_model(model: str) -> str:
    normalized = model.replace(".", "-").replace("_", "-")
    if normalized.startswith(("claude-opus-4-7", "claude-opus-4-8")):
        return "xhigh"
    return "high"


class ThinkingSignatureDetector(ActiveDetector):
    name = "thinking_signature"
    display_name = "思维签名验证"
    weight = 25.0

    def applies_to(self, model: str) -> bool:
        info = lookup_model(model)
        if info is None:
            return False
        return info.supports_extended_thinking or info.supports_adaptive_thinking

    async def run(self, client, model: str) -> DetectorResult:
        info = lookup_model(model)
        if info is None:  # belt-and-braces — applies_to should have caught this
            return self.skip("unknown model")

        # adaptive thinking uses a SEPARATE top-level `output_config.effort`
        # field — NOT a key under `thinking`. Putting effort inside thinking
        # is a 400 error ("Extra inputs are not permitted").
        extra: dict = {}
        if info.supports_extended_thinking:
            thinking = {"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS}
        elif info.supports_adaptive_thinking:
            # Opus 4.7 defaults `display` to "omitted"; explicit "summarized"
            # gives us thinking text to inspect. Opus adaptive probes need
            # xhigh for reliable signed-thinking emission on harder prompts.
            thinking = {"type": "adaptive", "display": "summarized"}
            extra["output_config"] = {"effort": _adaptive_effort_for_model(model)}
        else:
            return self.skip("model lacks thinking support")

        # Use NON-streaming. Empirically, streaming + adaptive + summarized on
        # Opus 4.7 silently drops the thinking block from the SSE stream
        # (verified: curl with `stream:true` shows only text events; curl
        # without stream returns full thinking+signature). Non-streaming gets
        # the same signature in `content[*].signature`, so detection power is
        # unchanged.
        try:
            _req, resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=MAX_TOKENS,
                thinking=thinking,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
                **extra,
            )
        except Exception as e:  # noqa: BLE001
            return self._result(
                "error",
                0.0,
                {
                    "thinking_params": thinking,
                    "output_config_sent": extra.get("output_config"),
                },
                error=str(e),
            )

        thinking_block_seen = False
        thinking_block_type: str | None = None
        signature_value = ""
        thinking_text_chars = 0
        content_block_types_seen: list[str] = []
        for block in resp.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if isinstance(btype, str):
                content_block_types_seen.append(btype)
            if btype in ("thinking", "redacted_thinking"):
                thinking_block_seen = True
                thinking_block_type = btype
                sig = block.get("signature")
                if isinstance(sig, str) and not signature_value:
                    signature_value = sig
                t = block.get("thinking")
                if isinstance(t, str):
                    thinking_text_chars += len(t)
        signature_received = bool(signature_value)
        stop_reason = resp.get("stop_reason")

        details: dict = {
            "thinking_params": thinking,
            "output_config_sent": extra.get("output_config"),
            "content_block_types_seen": content_block_types_seen,
            "thinking_block_seen": thinking_block_seen,
            "thinking_block_type": thinking_block_type,
            "thinking_text_chars": thinking_text_chars,
            "signature_received": signature_received,
            "signature_length": len(signature_value),
            "signature_prefix": signature_value[:32] if signature_value else "",
            "stop_reason": stop_reason,
        }

        if not thinking_block_seen:
            score = 0.0
            note = "no_thinking_block"
        elif not signature_received or not signature_value:
            score = 30.0
            note = "thinking_block_but_no_signature"
        elif len(signature_value) < SIGNATURE_MIN_LEN:
            score = 70.0
            note = "signature_too_short"
        else:
            score = 100.0
            note = "ok"
        details["evaluation"] = note

        status = "pass" if score >= 70 else "fail"
        return self._result(status, score, details)
