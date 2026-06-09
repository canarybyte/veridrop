"""Protocol-agnostic needle-in-haystack primitives for long-context detection.

The long-context detector probes whether a relay actually honors the model's
advertised context window, or silently truncates / routes to a smaller-window
model. Each protocol's detector calls into here to:

  1. Build a multi-needle haystack of approximately N tokens
  2. Send it through the relay (protocol-specific)
  3. Score how many needles the model can recall

Why three needles at 10% / 50% / 90%:
  - A single needle at the end is fooled by relays that pass head + tail and
    drop the middle. Three positions catches sliding-window truncation.
  - Multiple needles also smooth out occasional natural recall failures —
    even genuine 1M-context Claude misses ~1% of needles.

We don't use copyrighted text; the haystack is generated from random
template instantiations so it's deterministic-by-seed yet not in any
public dataset (which would let a model cheat via memorization).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass


# Empirical chars-per-token for our synthetic templated filler — varies
# significantly by tokenizer family. Live-measured against each provider's
# official API, gpt-4o-mini / claude-haiku-4-5 / (gemini TBD). Underestimating
# chars/token sends MORE actual tokens than the tier label promises (we
# overshoot the target — some buffer below means we won't blow past
# ctx_limit). Overestimating sends FEWER (we undershoot — coverage
# silently drops below the tier label).
#
# Calibration source:
#   OpenAI 6.0 ← gpt-4o-mini live: 32k→31397 / 100k→99144 (~99% accuracy)
#   Anthropic 5.25 ← claude-haiku-4-5 live 2026-05-05: 32k→36507 / 100k→114844
#                   (had been 6.0, overshot by 14%, blew past 200k ctx limit
#                    on 200k tier with HTTP 400 "prompt too long")
#   Gemini 5.5 ← unmeasured, conservative middle estimate
# Note Claude's chars/token varies with content length:
#   small (32k):  ~5.22 chars/tok (test data)
#   medium (100k): ~5.22 chars/tok
#   large (200k):  ~5.40-5.94 chars/tok (varies — denser BPE tokens at scale)
#
# 2026-05-05 second live test:
#   chars/tok=5.5 + buffer=1500 → 200k probe sent 201,534 tokens (still
#   over ctx_limit). Diagnosis: under-budgeted Anthropic's ~2000 system
#   overhead + ~250 question tokens. Fix combines lower chars/tok with
#   bigger buffer so even pessimistic scenarios land safely under limit.
_CHARS_PER_TOKEN_BY_PROTOCOL = {
    "openai":    6.0,
    "anthropic": 5.3,   # was 5.5 — too high, overshooting at 200k tier
    "gemini":    5.5,   # untested, conservative
}
_CHARS_PER_TOKEN_DEFAULT = 6.0  # fallback if protocol unknown


def _chars_per_token(protocol: str | None) -> float:
    if not protocol:
        return _CHARS_PER_TOKEN_DEFAULT
    return _CHARS_PER_TOKEN_BY_PROTOCOL.get(
        protocol.lower(), _CHARS_PER_TOKEN_DEFAULT
    )


# Advertised context window per model — tiers above the model's own limit
# would generate misleading "fail" results (the failure is the model, not
# the relay), so the detector skips them. Keys are alias prefixes so
# snapshot suffixes (e.g. gpt-4o-mini-2024-07-18) match.
#
# Verified against official docs 2026-05-05:
#   - Anthropic: docs.anthropic.com/en/docs/about-claude/models/overview
#       (latest comparison table + legacy models section)
#   - OpenAI:   learn.microsoft.com/en-us/azure/ai-services/openai/concepts/models
#       (platform.openai.com docs return 403 for automated fetches; the
#        Azure mirror tracks the same model spec sheet)
#   - Gemini:   ai.google.dev/gemini-api/docs/models/<model>
#       (per-model pages report Input token limit explicitly)
#
# Re-verify quarterly as new models ship — out-of-date entries cause two
# silent failure modes:
#   1. Limit too low  → detector skips a tier the model can actually handle
#                       (silent coverage loss, fewer truncation catches)
#   2. Limit too high → detector probes past the model's real limit, gets
#                       a 400 error, and misclassifies it as relay truncation
#                       (false positives on legitimate APIs)
_MODEL_CONTEXT_LIMITS = {
    # OpenAI (per Azure model spec sheet, mirrors platform.openai.com)
    "gpt-3.5-turbo":   16_385,
    "gpt-4o-mini":    128_000,
    "gpt-4o":         128_000,
    "gpt-4.1-mini":  1_047_576,
    "gpt-4.1-nano":  1_047_576,
    "gpt-4.1":       1_047_576,
    "gpt-5-nano":     272_000,   # 400k total / 272k input / 128k output
    "gpt-5-mini":     272_000,
    "gpt-5":          272_000,
    "o1-mini":        128_000,
    "o1":             200_000,
    "o3-mini":        200_000,   # was 128k in pre-2025-04 snapshots
    "o3":             200_000,
    # Anthropic — 1M is now GA on Opus/Sonnet 4.6+ without beta header.
    # Opus 4.7's pricing is flat $5/M with no >200k tier surcharge.
    "claude-haiku-4-5":   200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-8":   1_000_000,
    "claude-opus-4-7":   1_000_000,
    "claude-opus-4-6":   1_000_000,
    "claude-sonnet-4-5":  200_000,
    "claude-opus-4-5":    200_000,
    "claude-opus-4-1":    200_000,
    "claude-sonnet-4":    200_000,
    "claude-opus-4":      200_000,
    # Gemini — every current Gemini model reports 1,048,576 input tokens.
    # 1.5 Pro historically also 1M (despite some marketing materials
    # claiming 2M for "select customers"; default API is 1M).
    "gemini-3.1-pro":   1_048_576,
    "gemini-3-flash":   1_048_576,
    "gemini-3-pro":     1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro":   1_048_576,
    "gemini-1.5-flash": 1_048_576,
    "gemini-1.5-pro":   1_048_576,
}


def model_context_limit(model: str) -> int:
    """Advertised maximum input context for known models, in tokens.

    Returns 128_000 as a conservative default for unknown models so we
    don't accidentally probe a 16k model with a 200k haystack and flag
    the natural 400 error as a relay truncation.
    """
    if not model:
        return 128_000
    if model in _MODEL_CONTEXT_LIMITS:
        return _MODEL_CONTEXT_LIMITS[model]
    for prefix, limit in _MODEL_CONTEXT_LIMITS.items():
        if model.startswith(prefix):
            return limit
    return 128_000


# ---------- Tier strategies ----------

# Standard tiers: hardcoded, capped at 200k. Cheap probes that catch the
# overwhelming majority of relay truncation (transport-layer + mid-tier
# substitution). Total max cost ~$1 even on Opus 4.7.
STANDARD_TIERS = (32_000, 100_000, 200_000)


def tiers_for_model(ctx_limit: int) -> tuple[int, ...]:
    """Adaptive tiers covering low/mid/high of model's context window.

    For a 1M model returns ~(32k, 500k, 950k); for a 200k model returns
    ~(32k, 100k, 190k); for tiny 16k models returns ~(5k, 8k, 15k).

    Three principles:
      1. Always test 32k as tier 1 when the model can take it — that's
         where transport-layer truncation (nginx body cap, relay's own
         max_input) typically bites, and it's cheap regardless of model.
      2. 50% of context for tier 2 — middle of the range.
      3. 95% of context for tier 3 — close enough to the limit to catch
         "advertised X but actually Y<X" fraud, with 5% headroom for the
         question + small response tokens.

    Tiers within 1.5x of each other are deduplicated: a 64k model would
    otherwise produce (32k, 32k, 60k) which adds no extra coverage. After
    dedupe smaller models can return only 2 tiers, which is fine — the
    detector just runs fewer probes.

    This is the EXTREME tier strategy. The standard strategy
    (STANDARD_TIERS) caps at 200k regardless of model — cheaper, but
    blind to fraud at the 200k–1M range on big-context models.
    """
    raw = [
        min(32_000, int(ctx_limit * 0.30)),
        int(ctx_limit * 0.50),
        int(ctx_limit * 0.95),
    ]
    out: list[int] = []
    for t in raw:
        if t < 1_000:
            continue
        if not out or t >= out[-1] * 1.5:
            out.append(t)
    return tuple(out)


@dataclass
class Needle:
    """One fact embedded in the haystack that the model must recall.

    `position_pct` is where the sentence is inserted in the haystack
    (0.0 = start, 1.0 = end). `answer` is the unique identifier we expect
    to find verbatim in the model's response.
    """
    position_pct: float
    sentence: str       # inserted into haystack at position_pct
    answer: str         # the unique substring the model must reproduce
    label: str          # human-readable "what to search for" hint


def make_needles(seed: str) -> list[Needle]:
    """Three deterministic needles seeded by relay identity.

    Each Veridrop run gets its own seed (relay domain + timestamp), so
    needles are unique per check — a relay that memorized one set won't
    fool the next user. Three positions catch sliding-window truncation
    that a single end-needle would miss.
    """
    rng = random.Random(seed)
    return [
        Needle(
            position_pct=0.10,
            sentence=(
                "Important administrative note: the conference room access "
                "passcode for the duration of this document is {answer}. "
                "All attendees should retain this code for badge verification."
            ).format(answer=(passcode := _random_id(rng, "ALPHA"))),
            answer=passcode,
            label="conference room access passcode",
        ),
        Needle(
            position_pct=0.50,
            sentence=(
                "Editor's note inserted mid-document: the internal project "
                "codename for the upcoming deliverable is {answer}. This "
                "codename supersedes any prior placeholder identifiers."
            ).format(answer=(codename := _random_id(rng, "ATLAS"))),
            answer=codename,
            label="internal project codename",
        ),
        Needle(
            position_pct=0.90,
            sentence=(
                "Final operational note before closing: in case of emergency, "
                "the on-call engineer's reference identifier is {answer}. "
                "This identifier should be quoted in all incident reports."
            ).format(answer=(contact := _random_id(rng, "BRAVO"))),
            answer=contact,
            label="on-call engineer reference identifier",
        ),
    ]


def build_question(needles: list[Needle]) -> str:
    """The retrieval prompt appended after the haystack.

    Uses an explicit "report each on its own line" structure so we can
    parse model output reliably across response styles. Avoids leading
    instructions like "you must find" — those can prime the model to
    fabricate when content is truncated. We want honest "NOT FOUND"
    responses to discriminate truncation from comprehension failure.
    """
    bullets = "\n".join(f"  - The {n.label}" for n in needles)
    return (
        "\n\n---\n"
        "Carefully review the document above and locate the following "
        "identifiers, which were embedded as administrative notes:\n"
        f"{bullets}\n\n"
        "Reply with each identifier on its own line, in the order asked. "
        "If any identifier is not present in the document, write 'NOT FOUND' "
        "for that one. Do not guess or invent identifiers."
    )


def evaluate_recalls(response_text: str, needles: list[Needle]) -> list[bool]:
    """For each needle, did the model reproduce its `answer` in the response?

    Case-insensitive substring match — the answers are unique IDs (e.g.
    'ALPHA-3F8D-2C91') that won't collide with normal text by accident.
    Models sometimes paraphrase or list with different formatting; substring
    match tolerates this without giving false positives.
    """
    if not response_text:
        return [False] * len(needles)
    haystack = response_text.upper()
    return [n.answer.upper() in haystack for n in needles]


def assemble_haystack(
    target_tokens: int,
    needles: list[Needle],
    seed: str,
    protocol: str | None = None,
) -> str:
    """Build a haystack of approximately target_tokens with needles embedded
    at their target positions.

    `protocol` selects the tokenizer-family-specific chars/token calibration
    (OpenAI cl100k vs Anthropic vs Gemini). Without it the function defaults
    to OpenAI's ratio — historically the ratio used before per-protocol
    calibration; new callers should always pass protocol explicitly so the
    default never silently overshoots a different tokenizer.

    The filler text is synthetic — random template instantiations of
    plausible-looking factual sentences (fictional company reports, fake
    research summaries, etc.). This:
      - avoids copyright concerns
      - is deterministic per seed (same input → same haystack, reproducible)
      - is varied enough that simple compressors can't shrink it (~5–6 ch/tok)
      - is not in any public dataset, so models can't cheat via memorization

    Needles are placed at their position_pct points by inserting them
    between filler sentences. We don't slice mid-sentence to keep the
    document readable — model recall is sharper on coherent context.
    """
    chars_per_tok = _chars_per_token(protocol)
    target_chars = int(target_tokens * chars_per_tok)
    rng = random.Random(seed + ":haystack")

    sentences: list[str] = []
    char_count = 0
    while char_count < target_chars:
        s = _filler_sentence(rng)
        sentences.append(s)
        char_count += len(s) + 1  # +1 for joining space

    # Insert needles at target positions. We sort by position desc so
    # earlier insertions don't shift later indices.
    needle_inserts = sorted(
        [(int(n.position_pct * len(sentences)), n.sentence) for n in needles],
        key=lambda x: x[0],
        reverse=True,
    )
    for idx, sentence in needle_inserts:
        sentences.insert(idx, sentence)

    return " ".join(sentences)


def estimate_cost_usd(target_tokens: int, model: str) -> float:
    """Rough USD estimate for sending target_tokens of input.

    Hard-coded per-model price tiers — accurate enough for "is this $0.05 or
    $5?" guidance shown in the report's details. Real billing depends on
    provider tier transitions (e.g. Anthropic's >200k surcharge) which this
    helper does NOT model — only the base input rate. Conservative for users.
    """
    # USD per 1M input tokens, base tier — verified 2026-05-05 against
    # official pricing pages. Anthropic Opus 4.x dropped from $15 → $5
    # alongside the 1M context GA release, which our older estimates missed.
    rates = {
        # OpenAI (platform.openai.com pricing)
        "gpt-3.5-turbo":    0.50,
        "gpt-4o-mini":      0.15,
        "gpt-4o":           2.50,
        "gpt-4.1-nano":     0.10,
        "gpt-4.1-mini":     0.40,
        "gpt-4.1":          2.00,
        "gpt-5-nano":       0.05,
        "gpt-5-mini":       0.25,
        "gpt-5":            1.25,
        # Anthropic (docs.anthropic.com pricing — Opus 4.x now $5/M flat)
        "claude-haiku-4-5":  1.00,
        "claude-sonnet-4-6": 3.00,
        "claude-opus-4-8":   5.00,
        "claude-opus-4-7":   5.00,
        "claude-opus-4-6":   5.00,
        # Gemini (ai.google.dev pricing)
        "gemini-3-flash":   0.075,
        "gemini-3.1-pro":   1.25,
        "gemini-2.5-flash": 0.075,
        "gemini-2.5-pro":   1.25,
    }
    rate = rates.get(model)
    if rate is None:
        for prefix, r in rates.items():
            if model.startswith(prefix):
                rate = r
                break
        else:
            rate = 1.0  # fallback assumption
    return round(target_tokens * rate / 1_000_000, 4)


# ---------- Internal helpers ----------


def _random_id(rng: random.Random, prefix: str) -> str:
    """Generate a unique identifier like ALPHA-3F8D-2C91 — short enough to
    fit naturally in a sentence, long enough to never collide with normal
    text. Hex-only after prefix so the model can't "fix" it via spell-check."""
    return f"{prefix}-{rng.randrange(0xFFFF):04X}-{rng.randrange(0xFFFF):04X}"


_VOCAB = {
    "company": [
        "Acme Industries", "Blackstar Logistics", "Cardinal Robotics",
        "Delphi Technologies", "Evergreen Pharmaceuticals", "Falcon Aerospace",
        "Granite Holdings", "Halcyon Capital", "Iron Peak Mining",
        "Juno Biosciences", "Kestrel Manufacturing", "Lighthouse Energy",
        "Meridian Foods", "Northgate Construction", "Olympia Software",
    ],
    "department": [
        "finance", "operations", "research and development", "human resources",
        "legal", "marketing", "supply chain", "engineering", "compliance",
    ],
    "metric": [
        "operating revenue", "gross margin", "year-over-year growth",
        "customer acquisition cost", "throughput rate", "cycle time",
        "defect density", "utilization", "satisfaction score",
    ],
    "city": [
        "Geneva", "Singapore", "Buenos Aires", "Reykjavík", "Cape Town",
        "Vancouver", "Tashkent", "Auckland", "Edinburgh", "Marrakech",
        "Stockholm", "Hanoi", "Lima", "Tbilisi", "Kuala Lumpur",
    ],
    "topic": [
        "supply chain resilience", "regulatory compliance", "energy efficiency",
        "talent retention", "data governance", "risk management",
        "operational continuity", "quality assurance", "stakeholder engagement",
    ],
    "verb": [
        "outlined", "evaluated", "presented", "documented", "summarized",
        "introduced", "compared", "analyzed", "described", "highlighted",
    ],
}


def _filler_sentence(rng: random.Random) -> str:
    """One synthetic sentence ~80–180 chars. Mix of templates for variety
    (~20–45 tokens each) so the haystack converges on the target length
    in 500–10000 sentences depending on tier."""
    template = rng.choice([
        "In the {quarter} quarter of fiscal year {year}, the {department} "
        "department of {company} {verb} a {metric} review covering {topic}, "
        "with findings circulated to leadership for ratification.",

        "At the {year} industry symposium held in {city}, delegates {verb} "
        "the role of {topic} in shaping medium-term policy, citing data "
        "from the {company} compliance archive and the {department} working group.",

        "The internal {department} memorandum dated {month} {year} {verb} "
        "revisions to {company}'s {topic} guidelines, intended to align "
        "with the upcoming {metric} reporting cycle.",

        "Researchers at {company} {verb} the relationship between {topic} "
        "and {metric}, documenting interim results in a working paper "
        "circulated within the {department} unit during {quarter} of {year}.",

        "A cross-functional task force consisting of {department} and "
        "operations staff at {company} {verb} the {year} {topic} report, "
        "noting that the {metric} trajectory diverged from earlier projections.",
    ])
    return template.format(
        quarter=rng.choice(["first", "second", "third", "fourth"]),
        year=rng.randrange(2014, 2027),
        company=rng.choice(_VOCAB["company"]),
        department=rng.choice(_VOCAB["department"]),
        metric=rng.choice(_VOCAB["metric"]),
        city=rng.choice(_VOCAB["city"]),
        topic=rng.choice(_VOCAB["topic"]),
        verb=rng.choice(_VOCAB["verb"]),
        month=rng.choice([
            "January", "March", "May", "July", "September", "November",
        ]),
    )


# Public for tests
ANSWER_RE = re.compile(r"\b[A-Z]+-[0-9A-F]{4}-[0-9A-F]{4}\b")
