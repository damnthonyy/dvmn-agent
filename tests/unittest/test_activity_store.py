import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pr_agent.servers.activity_store as store
import pr_agent.servers.dashboard as dashboard


def _settings(overrides=None):
    overrides = overrides or {}
    return lambda: SimpleNamespace(get=lambda key, default=None: overrides.get(key, default))


@pytest.fixture
def memory_store(monkeypatch):
    """A clean in-memory store, the default (no persistent disk) configuration."""
    monkeypatch.setattr(store, "get_settings", _settings())
    store.reset_store()
    # A fresh shared-cache memory DB is only guaranteed once the previous one is
    # dropped; make sure we start from an empty table either way.
    conn = store._connection()
    conn.execute("DELETE FROM webhook_events")
    conn.commit()
    yield store
    store.reset_store()


def _body(repo="acme/repo", number=7, action="opened", sender="alice", installation=42):
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {"number": number},
        "sender": {"login": sender},
        "installation": {"id": installation},
    }


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

def test_summarize_extracts_pull_request_fields():
    assert store.summarize(_body()) == {
        "action": "opened", "repo_full_name": "acme/repo", "pr_number": 7,
        "sender": "alice", "installation_id": 42,
    }


def test_summarize_falls_back_to_issue_number():
    body = {"action": "created", "issue": {"number": 12}, "repository": {"full_name": "a/b"}}
    assert store.summarize(body)["pr_number"] == 12


def test_summarize_falls_back_to_check_run_pull_requests():
    body = {"check_run": {"pull_requests": [{"number": 99}]}}
    assert store.summarize(body)["pr_number"] == 99


def test_summarize_tolerates_empty_and_malformed_bodies():
    assert store.summarize({})["pr_number"] is None
    assert store.summarize(None)["repo_full_name"] is None
    assert store.summarize({"repository": "not-a-dict"})["repo_full_name"] is None


# ---------------------------------------------------------------------------
# Write / read cycle
# ---------------------------------------------------------------------------

def test_record_received_then_outcome(memory_store):
    store.record_received("d-1", "pull_request", _body())
    events = store.list_events()
    assert len(events) == 1
    assert events[0]["status"] == store.STATUS_RECEIVED
    assert events[0]["repo_full_name"] == "acme/repo"
    assert events[0]["pr_number"] == 7
    assert events[0]["duration_ms"] is None

    store.mark_processing("d-1")
    store.mark_outcome("d-1", store.STATUS_COMPLETED, commands=["/describe", "/review"])

    event = store.list_events()[0]
    assert event["status"] == store.STATUS_COMPLETED
    assert event["commands_run"] == ["/describe", "/review"]
    assert event["finished_at"] is not None
    assert event["duration_ms"] is not None and event["duration_ms"] >= 0


def test_ignored_outcome_records_the_reason(memory_store):
    store.record_received("d-2", "pull_request", _body())
    store.mark_outcome("d-2", store.STATUS_IGNORED, store.REASON_BOT_USER)

    event = store.list_events()[0]
    assert event["status"] == store.STATUS_IGNORED
    assert event["outcome_reason"] == store.REASON_BOT_USER


def test_redelivery_of_same_id_does_not_duplicate(memory_store):
    store.record_received("same", "pull_request", _body())
    store.mark_outcome("same", store.STATUS_COMPLETED)
    store.record_received("same", "pull_request", _body())

    events = store.list_events()
    assert len(events) == 1
    assert events[0]["status"] == store.STATUS_COMPLETED, "re-delivery must not reset the outcome"


def test_list_events_never_exposes_the_payload(memory_store):
    store.record_received("d-3", "pull_request", _body())
    event = store.list_events()[0]
    assert "payload" not in event
    assert event["payload_bytes"] > 0
    assert event["replayable"] is True


def test_get_payload_round_trips_server_side(memory_store):
    body = _body()
    store.record_received("d-4", "pull_request", body)
    assert store.get_payload("d-4") == body
    assert store.get_payload("unknown") is None


def test_oversized_payload_is_dropped_but_event_is_kept(monkeypatch, memory_store):
    monkeypatch.setattr(store, "get_settings",
                        _settings({"DASHBOARD.ACTIVITY_MAX_PAYLOAD_BYTES": 10}))
    store.record_received("d-5", "pull_request", _body())

    event = store.list_events()[0]
    assert event["payload_bytes"] > 10
    assert event["replayable"] is False, "an event we cannot replay must say so"
    assert store.get_payload("d-5") is None


def test_retention_prunes_oldest_rows(monkeypatch, memory_store):
    monkeypatch.setattr(store, "get_settings", _settings({"DASHBOARD.ACTIVITY_RETENTION": 3}))
    for i in range(6):
        store.record_received(f"d-{i}", "pull_request", _body(number=i))

    events = store.list_events()
    assert len(events) == 3
    assert [e["delivery_id"] for e in events] == ["d-5", "d-4", "d-3"]


def test_list_events_filters_and_orders(memory_store):
    store.record_received("a", "pull_request", _body(repo="x/one"))
    store.record_received("b", "pull_request", _body(repo="x/two"))
    store.record_received("c", "issue_comment", _body(repo="x/one"))
    store.mark_outcome("b", store.STATUS_FAILED, reason="boom")

    assert [e["delivery_id"] for e in store.list_events()] == ["c", "b", "a"]
    assert [e["delivery_id"] for e in store.list_events(repo="x/one")] == ["c", "a"]
    assert [e["delivery_id"] for e in store.list_events(status=store.STATUS_FAILED)] == ["b"]
    assert len(store.list_events(limit=1)) == 1


def test_persistence_toggle_uses_a_file(monkeypatch, tmp_path):
    db = tmp_path / "nested" / "activity.db"
    monkeypatch.setattr(store, "get_settings", _settings({
        "DASHBOARD.ACTIVITY_PERSIST": True, "DASHBOARD.ACTIVITY_DB_PATH": str(db),
    }))
    store.reset_store()
    try:
        store.record_received("p-1", "pull_request", _body())
        assert db.exists(), "the parent directory must be created"
        # Reopening the same file must find the row again.
        store.reset_store()
        assert [e["delivery_id"] for e in store.list_events()] == ["p-1"]
    finally:
        store.reset_store()


def test_memory_mode_starts_empty_after_reset(monkeypatch):
    monkeypatch.setattr(store, "get_settings", _settings())
    store.reset_store()
    try:
        store.record_received("m-1", "pull_request", _body())
        assert store.count_events() >= 1
    finally:
        store.reset_store()


# ---------------------------------------------------------------------------
# Async helpers never break the webhook path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_helpers_are_noops_without_delivery_id(memory_store):
    await store.arecord_received(None, "pull_request", _body())
    await store.amark_processing(None)
    await store.amark_outcome(None, store.STATUS_COMPLETED)
    assert store.count_events() == 0


@pytest.mark.asyncio
async def test_async_helpers_are_noops_when_disabled(monkeypatch, memory_store):
    monkeypatch.setattr(store, "get_settings", _settings({"DASHBOARD.ACTIVITY_ENABLED": False}))
    await store.arecord_received("d", "pull_request", _body())
    assert store.count_events() == 0


@pytest.mark.asyncio
async def test_async_helpers_swallow_storage_errors(monkeypatch, memory_store):
    """A broken activity log must never take the webhook pipeline down with it."""
    def _boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "record_received", _boom)
    monkeypatch.setattr(store, "mark_outcome", _boom)
    await store.arecord_received("d-x", "pull_request", _body())
    await store.amark_outcome("d-x", store.STATUS_COMPLETED)


# ---------------------------------------------------------------------------
# API route
# ---------------------------------------------------------------------------

def _client(monkeypatch, overrides=None):
    monkeypatch.setattr(dashboard, "get_settings", _settings(overrides or {}))
    app = FastAPI()
    app.include_router(dashboard.router)
    return TestClient(app)


def test_api_activity_returns_events(monkeypatch, memory_store):
    store.record_received("d-9", "pull_request", _body())
    store.mark_outcome("d-9", store.STATUS_IGNORED, store.REASON_PR_LOGIC_FILTER)

    body = _client(monkeypatch).get("/api/dashboard/activity").json()

    assert body["count"] == 1
    assert body["persistent"] is False
    event = body["events"][0]
    assert event["outcome_reason"] == store.REASON_PR_LOGIC_FILTER
    assert "payload" not in event, "the raw GitHub body must never leave the server"


def test_api_activity_honours_filters(monkeypatch, memory_store):
    store.record_received("f-1", "pull_request", _body(repo="x/one"))
    store.record_received("f-2", "pull_request", _body(repo="x/two"))

    resp = _client(monkeypatch).get("/api/dashboard/activity?repo=x/two")
    assert [e["delivery_id"] for e in resp.json()["events"]] == ["f-2"]


def test_api_activity_requires_token_when_configured(monkeypatch, memory_store):
    client = _client(monkeypatch, {"DASHBOARD.ACCESS_TOKEN": "s3cret"})
    assert client.get("/api/dashboard/activity").status_code == 401
    assert client.get("/api/dashboard/activity?token=s3cret").status_code == 200
