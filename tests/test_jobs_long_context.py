"""include_long_context flag plumbing: form field → jobs.submit → _run → cfg."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from relay_detector.core.models import ExecutionConfig
from web import jobs


@pytest.fixture
def isolated_jobs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Avoid touching real /opt/veridrop/web_data/jobs during tests."""
    fake_dir = tmp_path / "jobs"
    fake_dir.mkdir()
    monkeypatch.setattr(jobs, "JOBS_DIR", fake_dir)
    # Reset the shared state that survives across tests.
    monkeypatch.setattr(jobs, "_JOBS", {})
    return fake_dir


class _CapturedOutcome:
    """Stand-in for runner.run() return — minimal shape jobs._run reads."""

    def __init__(self):
        self.results = []
        self.performance = None


async def _capture_cfg(cfg_holder: list, *args):
    """Drop-in for _run_anthropic/_run_openai/_run_gemini that captures the
    passed-in ExecutionConfig and returns a minimal valid outcome shape."""
    # signature: (base_url, api_key, model, cfg)
    cfg_holder.append(args[3])
    return _CapturedOutcome()


@pytest.mark.asyncio
async def test_long_context_flag_default_false(
    isolated_jobs_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """Default submission (no opt-in) sets include_long_context=False."""
    captured: list[ExecutionConfig] = []

    async def fake_anthropic(*args, **kwargs):
        return await _capture_cfg(captured, *args)

    monkeypatch.setattr(jobs, "_run_anthropic", fake_anthropic)
    # Avoid file I/O for the report write step
    monkeypatch.setattr(jobs, "report_path", lambda *a, **k: isolated_jobs_dir / "x.json")

    job_id = await jobs.submit(
        "https://relay.example",
        "sk-test",
        "claude-haiku-4-5",
        "quick",
        protocol="anthropic",
    )
    # Wait for the fire-and-forget task to finish
    for _ in range(20):
        if (await jobs.get(job_id)) and (await jobs.get(job_id)).status in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    assert len(captured) == 1
    assert captured[0].include_long_context is False


@pytest.mark.asyncio
async def test_long_context_flag_true_propagates(
    isolated_jobs_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """When opt-in flag is True, ExecutionConfig.include_long_context is True
    in the cfg passed to the per-protocol runner. This is the smoke test
    that lets the Web checkbox actually trigger the long-context probes."""
    captured: list[ExecutionConfig] = []

    async def fake_openai(*args, **kwargs):
        return await _capture_cfg(captured, *args)

    monkeypatch.setattr(jobs, "_run_openai", fake_openai)
    monkeypatch.setattr(jobs, "report_path", lambda *a, **k: isolated_jobs_dir / "x.json")

    job_id = await jobs.submit(
        "https://relay.example",
        "sk-test",
        "gpt-4o-mini",
        "full",
        protocol="openai",
        include_long_context=True,
    )
    for _ in range(20):
        if (await jobs.get(job_id)) and (await jobs.get(job_id)).status in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    assert len(captured) == 1
    assert captured[0].include_long_context is True
