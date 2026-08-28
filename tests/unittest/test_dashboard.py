import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import pr_agent.servers.dashboard as dashboard


def _make_request(query=None, headers=None):
    return SimpleNamespace(
        query_params=query or {},
        headers=headers or {},
    )


# ---------------------------------------------------------------------------
# _check_access
# ---------------------------------------------------------------------------

def test_check_access_open_when_token_unset(monkeypatch):
    monkeypatch.setattr(dashboard, "get_settings", lambda: SimpleNamespace(get=lambda *a, **k: ""))
    # Should not raise
    dashboard._check_access(_make_request())


def test_check_access_rejects_missing_token(monkeypatch):
    monkeypatch.setattr(dashboard, "get_settings", lambda: SimpleNamespace(get=lambda *a, **k: "secret"))
    with pytest.raises(HTTPException) as exc:
        dashboard._check_access(_make_request())
    assert exc.value.status_code == 401


def test_check_access_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(dashboard, "get_settings", lambda: SimpleNamespace(get=lambda *a, **k: "secret"))
    with pytest.raises(HTTPException):
        dashboard._check_access(_make_request(query={"token": "nope"}))


def test_check_access_accepts_query_token(monkeypatch):
    monkeypatch.setattr(dashboard, "get_settings", lambda: SimpleNamespace(get=lambda *a, **k: "secret"))
    dashboard._check_access(_make_request(query={"token": "secret"}))


def test_check_access_accepts_header_token(monkeypatch):
    monkeypatch.setattr(dashboard, "get_settings", lambda: SimpleNamespace(get=lambda *a, **k: "secret"))
    dashboard._check_access(_make_request(headers={"X-Dashboard-Token": "secret"}))


# ---------------------------------------------------------------------------
# collect_dashboard_data
# ---------------------------------------------------------------------------

def _fake_pr(number, title, author, html_url):
    return SimpleNamespace(
        number=number,
        title=title,
        user=SimpleNamespace(login=author),
        created_at=datetime(2026, 1, 2, 3, 4, 5),
        html_url=html_url,
    )


def _patch_settings(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "get_settings",
        lambda: SimpleNamespace(get=lambda key, default=None: default),
    )


def test_collect_dashboard_data_happy_path(monkeypatch):
    _patch_settings(monkeypatch)

    integration = mock.MagicMock()
    integration.get_installations.return_value = [SimpleNamespace(id=111)]
    integration.get_access_token.return_value = SimpleNamespace(token="tok")
    monkeypatch.setattr(dashboard, "_build_app_integration", lambda: integration)

    repos = [{"full_name": "acme/repo1", "html_url": "https://github.com/acme/repo1"}]
    monkeypatch.setattr(dashboard, "_list_installation_repos", lambda client: repos)

    repo_obj = mock.MagicMock()
    repo_obj.get_pulls.return_value = [
        _fake_pr(1, "Fix bug", "alice", "https://github.com/acme/repo1/pull/1"),
    ]
    fake_client = mock.MagicMock()
    fake_client.get_repo.return_value = repo_obj
    monkeypatch.setattr(dashboard, "Github", lambda *a, **k: fake_client)

    data = dashboard.collect_dashboard_data()

    assert len(data) == 1
    assert data[0]["repo_full_name"] == "acme/repo1"
    assert data[0]["pulls"][0]["number"] == 1
    assert data[0]["pulls"][0]["author"] == "alice"
    assert data[0]["pulls"][0]["html_url"].endswith("/pull/1")


def test_collect_dashboard_data_skips_repo_on_error(monkeypatch):
    _patch_settings(monkeypatch)

    integration = mock.MagicMock()
    integration.get_installations.return_value = [SimpleNamespace(id=222)]
    integration.get_access_token.return_value = SimpleNamespace(token="tok")
    monkeypatch.setattr(dashboard, "_build_app_integration", lambda: integration)

    repos = [
        {"full_name": "acme/bad", "html_url": "https://github.com/acme/bad"},
        {"full_name": "acme/good", "html_url": "https://github.com/acme/good"},
    ]
    monkeypatch.setattr(dashboard, "_list_installation_repos", lambda client: repos)

    good_repo = mock.MagicMock()
    good_repo.get_pulls.return_value = []

    def get_repo(full_name):
        if full_name == "acme/bad":
            raise RuntimeError("boom")
        return good_repo

    fake_client = mock.MagicMock()
    fake_client.get_repo.side_effect = get_repo
    monkeypatch.setattr(dashboard, "Github", lambda *a, **k: fake_client)

    data = dashboard.collect_dashboard_data()

    # Both repos are present; the failing one simply has no pulls and does not crash.
    assert {d["repo_full_name"] for d in data} == {"acme/bad", "acme/good"}
    bad = next(d for d in data if d["repo_full_name"] == "acme/bad")
    assert bad["pulls"] == []


def test_collect_dashboard_data_returns_empty_when_no_installations(monkeypatch):
    _patch_settings(monkeypatch)

    integration = mock.MagicMock()
    integration.get_installations.return_value = []
    monkeypatch.setattr(dashboard, "_build_app_integration", lambda: integration)

    assert dashboard.collect_dashboard_data() == []


# ---------------------------------------------------------------------------
# Cache / get_dashboard_data
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cache():
    dashboard.reset_cache()
    yield
    dashboard.reset_cache()


def _patch_settings_with(monkeypatch, overrides):
    """Patch get_settings so .get(key, default) honours `overrides` then the default."""
    monkeypatch.setattr(
        dashboard,
        "get_settings",
        lambda: SimpleNamespace(get=lambda key, default=None: overrides.get(key, default)),
    )


def _counting_collector(payloads):
    calls = {"n": 0}

    def _collect():
        calls["n"] += 1
        value = payloads[min(calls["n"] - 1, len(payloads) - 1)]
        if isinstance(value, Exception):
            raise value
        return value

    return _collect, calls


async def test_get_dashboard_data_collects_then_serves_from_cache(monkeypatch):
    _patch_settings(monkeypatch)
    collect, calls = _counting_collector([[{"repo_full_name": "acme/a", "pulls": [{"number": 1}]}]])
    monkeypatch.setattr(dashboard, "collect_dashboard_data", collect)

    first = await dashboard.get_dashboard_data()
    second = await dashboard.get_dashboard_data()

    assert calls["n"] == 1, "second call within the TTL must not re-hit GitHub"
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["repo_count"] == 1
    assert second["open_pr_count"] == 1
    assert second["stale"] is False
    assert second["collected_at"] is not None


async def test_get_dashboard_data_force_refresh_bypasses_cache(monkeypatch):
    _patch_settings(monkeypatch)
    collect, calls = _counting_collector([[], []])
    monkeypatch.setattr(dashboard, "collect_dashboard_data", collect)

    await dashboard.get_dashboard_data()
    payload = await dashboard.get_dashboard_data(force_refresh=True)

    assert calls["n"] == 2
    assert payload["cached"] is False


async def test_get_dashboard_data_refreshes_once_ttl_expired(monkeypatch):
    _patch_settings_with(monkeypatch, {"DASHBOARD.CACHE_TTL_SECONDS": 0})
    collect, calls = _counting_collector([[], []])
    monkeypatch.setattr(dashboard, "collect_dashboard_data", collect)

    await dashboard.get_dashboard_data()
    await dashboard.get_dashboard_data()

    assert calls["n"] == 2


async def test_get_dashboard_data_serves_stale_snapshot_on_refresh_failure(monkeypatch):
    _patch_settings(monkeypatch)
    good = [{"repo_full_name": "acme/a", "pulls": []}]
    collect, _ = _counting_collector([good, RuntimeError("github down")])
    monkeypatch.setattr(dashboard, "collect_dashboard_data", collect)

    await dashboard.get_dashboard_data()
    payload = await dashboard.get_dashboard_data(force_refresh=True)

    assert payload["stale"] is True
    assert payload["repo_count"] == 1, "the previous snapshot must survive a failed refresh"


async def test_get_dashboard_data_propagates_failure_without_cache(monkeypatch):
    _patch_settings(monkeypatch)
    collect, _ = _counting_collector([RuntimeError("github down")])
    monkeypatch.setattr(dashboard, "collect_dashboard_data", collect)

    with pytest.raises(RuntimeError):
        await dashboard.get_dashboard_data()


async def test_concurrent_callers_share_a_single_refresh(monkeypatch):
    _patch_settings(monkeypatch)
    collect, calls = _counting_collector([[], []])
    monkeypatch.setattr(dashboard, "collect_dashboard_data", collect)

    results = await asyncio.gather(*(dashboard.get_dashboard_data() for _ in range(5)))

    assert calls["n"] == 1, "the lock must collapse a stampede into one collection"
    assert sum(1 for r in results if r["cached"] is False) == 1


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------

def _client(monkeypatch, overrides=None):
    _patch_settings_with(monkeypatch, overrides or {})
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app)


def test_api_repos_returns_payload(monkeypatch):
    monkeypatch.setattr(
        dashboard, "collect_dashboard_data",
        lambda: [{"repo_full_name": "acme/a", "pulls": [{"number": 1}, {"number": 2}]}],
    )
    resp = _client(monkeypatch).get("/api/dashboard/repos")

    assert resp.status_code == 200
    body = resp.json()
    assert body["repo_count"] == 1
    assert body["open_pr_count"] == 2
    assert body["repos"][0]["repo_full_name"] == "acme/a"


def test_api_refresh_bypasses_cache(monkeypatch):
    collect, calls = _counting_collector([[], []])
    monkeypatch.setattr(dashboard, "collect_dashboard_data", collect)
    client = _client(monkeypatch)

    client.get("/api/dashboard/repos")
    resp = client.post("/api/dashboard/refresh")

    assert resp.status_code == 200
    assert calls["n"] == 2
    assert resp.json()["cached"] is False


def test_api_repos_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(dashboard, "collect_dashboard_data", lambda: [])
    client = _client(monkeypatch, {"DASHBOARD.ACCESS_TOKEN": "s3cret"})

    assert client.get("/api/dashboard/repos").status_code == 401
    assert client.get("/api/dashboard/repos?token=s3cret").status_code == 200
    assert client.get(
        "/api/dashboard/repos", headers={"X-Dashboard-Token": "s3cret"}
    ).status_code == 200


def test_api_repos_returns_503_on_misconfiguration(monkeypatch):
    def _boom():
        raise ValueError("The dashboard requires GitHub App deployment")

    monkeypatch.setattr(dashboard, "collect_dashboard_data", _boom)
    resp = _client(monkeypatch).get("/api/dashboard/repos")

    assert resp.status_code == 503
    assert "GitHub App deployment" in resp.json()["detail"]


async def test_collection_runs_off_the_event_loop_thread(monkeypatch):
    """The webhook server shares this event loop: collection must not block it."""
    _patch_settings(monkeypatch)
    seen = {}

    def _collect():
        seen["thread"] = threading.current_thread().ident
        return []

    monkeypatch.setattr(dashboard, "collect_dashboard_data", _collect)
    await dashboard.get_dashboard_data()

    assert seen["thread"] != threading.current_thread().ident
