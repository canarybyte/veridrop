"""Tests for /r/{job_id} SEO meta — title diversity drives long-tail SEO,
so model name must appear in the page title, not just domain + verdict.
Without per-model variation, two reports on the same relay collide on the
same title text and Google deduplicates them out of the index.
"""

from __future__ import annotations

from web.server import _seo_meta_for_report


def _base_report(**overrides):
    """Minimal finished-report shape that _seo_meta_for_report consumes."""
    report = {
        "base_url": "https://www.fucheers.top/v1",
        "protocol": "openai",
        "target_model": "gpt-5.5",
        "total_score": 91.0,
        "verdict": "marginal",
        "results": [
            {"status": "pass"} for _ in range(7)
        ] + [
            {"status": "fail"} for _ in range(2)
        ],
    }
    report.update(overrides)
    return report


def test_seo_title_includes_target_model():
    """Each report's title must embed the detected model so reports across
    the same relay become distinct indexable pages."""
    meta = _seo_meta_for_report(_base_report())
    title = meta["seo_title"]

    assert "www.fucheers.top" in title
    assert "OpenAI" in title
    assert "gpt-5.5" in title, f"model missing from title: {title!r}"
    assert "91/100" in title
    assert "存在风险" in title


def test_seo_title_falls_back_when_target_model_missing():
    """Legacy reports / probe failures may not have target_model. Title
    must still render coherently — must NOT print empty 'OpenAI 中转站  检测:'."""
    meta = _seo_meta_for_report(_base_report(target_model=""))
    title = meta["seo_title"]

    # No leading/trailing extra space, no "  " (double space) artifact
    assert "  " not in title, f"double-space artifact in fallback title: {title!r}"
    assert "www.fucheers.top" in title
    assert "OpenAI 中转站检测" in title
    assert "91/100" in title


def test_seo_title_distinct_per_model_same_relay():
    """Regression for the long-tail SEO intent: same domain + same score
    + different model should yield different titles (otherwise Google
    de-duplicates them and we lose the indexable surface)."""
    a = _seo_meta_for_report(_base_report(target_model="gpt-5.5"))["seo_title"]
    b = _seo_meta_for_report(_base_report(target_model="gpt-5.4-mini"))["seo_title"]
    c = _seo_meta_for_report(_base_report(target_model="gpt-4o"))["seo_title"]

    assert a != b != c
    assert "gpt-5.5" in a
    assert "gpt-5.4-mini" in b
    assert "gpt-4o" in c


def test_seo_title_respects_155_char_cap():
    """The 155-char cap is a hard SEO ceiling — long model snapshot IDs
    must not push the title over."""
    meta = _seo_meta_for_report(_base_report(target_model="gpt-5.5-2026-04-23"))
    assert len(meta["seo_title"]) <= 155
    assert "gpt-5.5-2026-04-23" in meta["seo_title"]


def test_seo_description_still_mentions_model():
    """The description block already mentioned model before this change —
    guarding it here so a future title refactor doesn't accidentally drop
    it from the meta description."""
    meta = _seo_meta_for_report(_base_report(target_model="gpt-5.5"))
    assert "gpt-5.5" in meta["seo_description"]
