"""IntegrityDetector — DESIGN.md §3.9.

Active. Five sub-checks, each worth 20 points:
  1. stream vs non-stream text similarity (rapidfuzz.ratio >= 85)
  2. char/token ratio is in a sane range
  3. streaming output_tokens (cumulative final value) close to non-stream
  4. stop_reason on streamed response is a known enum value
  5. input_tokens consistency between stream and non-stream
     (added M2.1 — same prompt + temp=0 must report nearly identical input;
     a 10x divergence we observed on a relay station strongly suggests an
     injected system prompt or inflated billing)

In quick mode this detector is excluded entirely (see config.MODE_DETECTORS).
In standard/full it runs the same — long-output check is reserved for v2.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from ....core.models import DetectorResult, Mode
from .base import ActiveDetector


# Determinism-friendly probe: short, structured, easy to compare textually.
PROBE_PROMPT = (
    "Reply with exactly this JSON literal and nothing else: "
    '{"verify":"abc123","n":42}'
)
PROBE_MAX_TOKENS = 80

# Heuristic char/token bounds. English text averages ~3-4 chars/token; we
# allow a wide window since responses can include whitespace/punctuation.
# Lower bound was 1.5 originally but Opus 4.7's new tokenizer + dense JSON
# output gave 1.44 char/token on a real official-API run, so 1.2 keeps that
# legitimate case while still rejecting "1 char per token" inflation by a
# relay that fakes higher token counts.
CHARS_PER_TOKEN_MIN = 1.2
CHARS_PER_TOKEN_MAX = 10.0

SIMILARITY_PASS = 85.0

# input_tokens between stream/non-stream may differ by at most this fraction
# of the non-stream baseline (or the absolute floor below, whichever is larger).
INPUT_TOKEN_TOLERANCE_FRAC = 0.20
INPUT_TOKEN_TOLERANCE_FLOOR = 3

SUB_CHECK_WEIGHT = 20.0  # 5 checks × 20 = 100


class IntegrityDetector(ActiveDetector):
    name = "integrity"
    display_name = "响应完整性"
    weight = 5.0
    category = "consistency"
    description = "验证流式响应完整性与事件序列"
    modes = {Mode.STANDARD, Mode.FULL}

    async def run(self, client, model: str) -> DetectorResult:
        details: dict = {}

        # --- Pass 1: non-streaming -------------------------------------
        try:
            _req, ns_resp, _h, _lat = await client.messages_create(
                model=model,
                max_tokens=PROBE_MAX_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
            )
        except Exception as e:  # noqa: BLE001
            return self._result(
                "error", 0.0, {"phase": "non_stream"}, error=str(e)
            )

        ns_text = _join_text_blocks(ns_resp.get("content"))
        ns_usage = ns_resp.get("usage") or {}
        ns_input = ns_usage.get("input_tokens") or 0
        ns_output = ns_usage.get("output_tokens") or 0
        details["ns_text"] = ns_text[:200]
        details["ns_usage"] = {"input": ns_input, "output": ns_output}

        # --- Pass 2: streaming -----------------------------------------
        stream_text = ""
        stream_stop_reason = None
        stream_message_delta_usage: dict | None = None
        stream_message_start_input_tokens: int | None = None
        stream_message_start_seen = False
        try:
            async for ev, _elapsed in client.messages_stream(
                model=model,
                max_tokens=PROBE_MAX_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": PROBE_PROMPT}],
            ):
                if ev.event == "message_start":
                    stream_message_start_seen = True
                    msg = ev.data.get("message") or {}
                    msu = msg.get("usage") or {}
                    if isinstance(msu.get("input_tokens"), int):
                        stream_message_start_input_tokens = msu["input_tokens"]
                elif ev.event == "content_block_delta":
                    delta = ev.data.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        stream_text += delta.get("text") or ""
                elif ev.event == "message_delta":
                    delta = ev.data.get("delta") or {}
                    if "stop_reason" in delta:
                        stream_stop_reason = delta["stop_reason"]
                    u = ev.data.get("usage")
                    if isinstance(u, dict):
                        stream_message_delta_usage = u
        except Exception as e:  # noqa: BLE001
            return self._result(
                "error",
                0.0,
                {"phase": "stream", **details},
                error=str(e),
            )

        details["stream_text"] = stream_text[:200]
        details["stream_stop_reason"] = stream_stop_reason
        details["stream_message_delta_usage"] = stream_message_delta_usage
        details["stream_message_start_input_tokens"] = stream_message_start_input_tokens
        details["stream_message_start_seen"] = stream_message_start_seen

        # --- Scoring ---------------------------------------------------
        score = 0.0
        sub: dict[str, dict] = {}

        # Check 1: text similarity
        ratio = (
            fuzz.ratio(ns_text, stream_text)
            if ns_text and stream_text
            else 0.0
        )
        sub["similarity"] = {
            "ratio": round(ratio, 1),
            "pass": ratio >= SIMILARITY_PASS,
        }
        if ratio >= SIMILARITY_PASS:
            score += SUB_CHECK_WEIGHT

        # Check 2: char/token ratio reasonable
        if ns_output > 0 and ns_text:
            cpt = len(ns_text) / ns_output
            ok = CHARS_PER_TOKEN_MIN <= cpt <= CHARS_PER_TOKEN_MAX
            sub["char_per_token"] = {"value": round(cpt, 2), "pass": ok}
            if ok:
                score += SUB_CHECK_WEIGHT
        else:
            sub["char_per_token"] = {"value": None, "pass": False}

        # Check 3: streaming output_tokens cumulative & close to non-stream.
        if stream_message_delta_usage and isinstance(
            stream_message_delta_usage.get("output_tokens"), int
        ):
            stream_out = stream_message_delta_usage["output_tokens"]
            tolerance = max(2, int(ns_output * 0.5))
            ok = abs(stream_out - ns_output) <= tolerance and stream_out > 0
            sub["stream_output_tokens"] = {
                "stream": stream_out,
                "ns": ns_output,
                "pass": ok,
            }
            if ok:
                score += SUB_CHECK_WEIGHT
        else:
            sub["stream_output_tokens"] = {"stream": None, "pass": False}

        # Check 4: stop_reason valid
        ok = stream_stop_reason in {"end_turn", "max_tokens"}
        sub["stop_reason"] = {"value": stream_stop_reason, "pass": ok}
        if ok:
            score += SUB_CHECK_WEIGHT

        # Check 5: input_tokens consistency.
        # Same prompt + temp=0 must report (near-)identical input. A large
        # divergence usually means the relay injected a system prompt for
        # streaming, or is inflating billing.
        if stream_message_start_input_tokens is not None and ns_input > 0:
            tolerance = max(
                INPUT_TOKEN_TOLERANCE_FLOOR,
                int(ns_input * INPUT_TOKEN_TOLERANCE_FRAC),
            )
            diff = abs(stream_message_start_input_tokens - ns_input)
            ok = diff <= tolerance
            sub["input_tokens"] = {
                "ns": ns_input,
                "stream": stream_message_start_input_tokens,
                "diff": diff,
                "tolerance": tolerance,
                "pass": ok,
            }
            if ok:
                score += SUB_CHECK_WEIGHT
        else:
            sub["input_tokens"] = {
                "ns": ns_input,
                "stream": stream_message_start_input_tokens,
                "pass": False,
            }

        details["sub_checks"] = sub
        status = "pass" if score >= 70 else "fail"
        return self._result(status, score, details)


def _join_text_blocks(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)
