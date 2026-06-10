from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException

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
