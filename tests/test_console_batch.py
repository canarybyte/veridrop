"""批量检测接口：每个模型一个任务，任一校验失败则整体不提交。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web import console, repository
from web.probe_catalog import build_catalog
from web.server import app

CREDENTIAL = {
    "id": "cred1",
    "name": "relay",
    "base_url": "https://relay.example.com",
    "api_key_enc": "ignored",
}


@pytest.fixture
def submitted(monkeypatch):
    calls: list[dict] = []

    async def fake_get_secret(credential_id: str):
        return dict(CREDENTIAL) if credential_id == "cred1" else None

    async def fake_submit(base_url, api_key, model, **kwargs):
        calls.append({"model": model, **kwargs})
        return f"job-{len(calls)}"

    monkeypatch.setattr(repository, "get_credential_secret", fake_get_secret)
    monkeypatch.setattr(console, "decrypt_api_key", lambda _enc: "sk-test")
    monkeypatch.setattr(console.jobs, "submit", fake_submit)
    return calls


def _probe_names(protocol: str, model: str) -> list[str]:
    return [item["name"] for item in build_catalog(protocol, model)["probes"]]


def test_batch_starts_one_job_per_unique_model(submitted):
    probes = _probe_names("anthropic", "claude-sonnet-4-6")[:2]
    with TestClient(app) as client:
        response = client.post(
            "/app/api/detect/batch",
            json={
                "credential_id": "cred1",
                "models": ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-4-6"],
                "probes": probes,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert [item["model"] for item in body["items"]] == [
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ]
    assert [item["job_id"] for item in body["items"]] == ["job-1", "job-2"]
    assert len(submitted) == 2
    assert all(call["protocol"] == "anthropic" for call in submitted)
    assert all(call["selected_probes"] == probes for call in submitted)


def test_batch_rejects_mixed_protocols_without_submitting(submitted):
    probes = _probe_names("anthropic", "claude-sonnet-4-6")[:1]
    with TestClient(app) as client:
        response = client.post(
            "/app/api/detect/batch",
            json={
                "credential_id": "cred1",
                "models": ["claude-sonnet-4-6", "gpt-4o"],
                "probes": probes,
            },
        )

    assert response.status_code == 422
    assert submitted == []


def test_batch_rejects_unknown_probe_without_submitting(submitted):
    with TestClient(app) as client:
        response = client.post(
            "/app/api/detect/batch",
            json={
                "credential_id": "cred1",
                "models": ["claude-sonnet-4-6", "claude-haiku-4-5"],
                "probes": ["no_such_probe"],
            },
        )

    assert response.status_code == 422
    assert submitted == []


def test_batch_requires_models_and_probes(submitted):
    with TestClient(app) as client:
        empty_models = client.post(
            "/app/api/detect/batch",
            json={"credential_id": "cred1", "models": [], "probes": ["x"]},
        )
        no_probes = client.post(
            "/app/api/detect/batch",
            json={"credential_id": "cred1", "models": ["claude-sonnet-4-6"], "probes": []},
        )

    assert empty_models.status_code == 422
    assert no_probes.status_code == 422
    assert submitted == []


def test_batch_unknown_credential_returns_404(submitted):
    with TestClient(app) as client:
        response = client.post(
            "/app/api/detect/batch",
            json={
                "credential_id": "missing",
                "models": ["claude-sonnet-4-6"],
                "probes": _probe_names("anthropic", "claude-sonnet-4-6")[:1],
            },
        )

    assert response.status_code == 404
    assert submitted == []
