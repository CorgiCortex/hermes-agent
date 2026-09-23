"""Invariant test: a completed webhook delivery closes its session.

Regression guard for the ghost-session leak.  Webhook deliveries create a
unique one-shot session (``delivery_id`` baked into the session key), but the
adapter historically fired ``handle_message`` without ever ending the session.
``SessionDB.prune_sessions`` only reaps rows where ``ended_at IS NOT NULL``, so
every webhook session stayed unprunable and state.db grew without bound (this
was the primary driver of the SQLite lock-contention gateway outage).

The invariant asserted here is a *behavior contract*, not a snapshot: once a
webhook delivery's agent run completes, the session row for that delivery must
have ``ended_at`` set — mirroring how a cron run closes its session with
``end_session(..., "cron_complete")``.

CRITICAL: these tests go through the REAL ``handle_message`` →
``_process_message_background`` → ``on_processing_complete`` pipeline (only the
runner-side ``_message_handler`` is stubbed, exactly the seam the live gateway
injects).  ``handle_message`` is fire-and-forget — it spawns the background
task and returns before the run starts — so any close bolted around
``handle_message`` itself runs BEFORE the session row exists and silently
no-ops.  A test that fakes ``handle_message`` to create the row synchronously
masks exactly that bug (the first version of this fix shipped that way).
"""

import asyncio

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.session import SessionSource, SessionStore


def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


class _FakeRunner:
    """Minimal gateway runner surface the webhook close path depends on.

    Wires a real ``SessionStore`` (which owns a real ``SessionDB``) and reuses
    that same ``SessionDB`` as ``_session_db`` so the row created at routing
    time is the row the close path ends — exactly the wiring the live gateway
    has (``self.session_store`` + ``self._session_db``).
    """

    def __init__(self, store: SessionStore):
        self.session_store = store
        self._session_db = store._db
        self.evicted_session_keys = []

    def _session_key_for_source(self, source: SessionSource) -> str:
        return self.session_store._generate_session_key(source)

    def _evict_cached_agent(self, session_key: str) -> None:
        self.evicted_session_keys.append(session_key)


def _make_store(tmp_path) -> SessionStore:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    config = GatewayConfig(
        platforms={Platform.WEBHOOK: PlatformConfig(enabled=True)}
    )
    store = SessionStore(sessions_dir=sessions_dir, config=config)
    assert store._db is not None, "test requires a real SessionDB"
    return store


async def _drain_background_tasks(adapter: WebhookAdapter, timeout: float = 5.0) -> None:
    """Wait for the adapter's spawned processing task(s) to finish."""
    deadline = asyncio.get_event_loop().time() + timeout
    while adapter._background_tasks and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    # One extra tick for done-callbacks to run.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_completed_webhook_delivery_closes_its_session(tmp_path, outcome):
    """After a webhook run finishes (REAL dispatch path), ended_at is set."""
    store = _make_store(tmp_path)
    runner = _FakeRunner(store)

    adapter = _make_adapter(
        {
            "alerts": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "Alert: {message}",
                "deliver": "log",
                "serial_key": "scope",
            }
        }
    )
    adapter.gateway_runner = runner

    # Stub the RUNNER-side handler (the seam the live gateway injects) — the
    # adapter's own handle_message / _process_message_background pipeline runs
    # for real, including the fire-and-forget task spawn and the
    # on_processing_complete hook.  The handler creates the session row, just
    # like GatewayRunner._handle_message does at routing time.
    created = {}
    release = asyncio.Event()
    both_started = asyncio.Event()

    async def _message_handler(event: MessageEvent):
        entry = store.get_or_create_session(event.source)
        created[event.source.chat_id] = (entry.session_id, event)
        if len(created) == 2:
            both_started.set()
        await release.wait()
        if outcome == "failure":
            raise RuntimeError("intentional worker failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return ""

    adapter._message_handler = _message_handler
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        async def deliver(scope, attempt):
            response = await client.post("/webhooks/alerts", json={"scope": scope, "message": "review"}, headers={"X-Request-ID": attempt})
            assert response.status == 202
            return await response.json()

        first = await deliver("instrument:A", "a1")
        busy = await deliver("instrument:A", "a2")
        second = await deliver("instrument:B", "b1")
        assert first["status"] == second["status"] == "accepted"
        assert busy["status"] == "busy"
        assert busy["active_chat_id"] == first["active_chat_id"]
        assert second["active_chat_id"] != first["active_chat_id"]
        await asyncio.wait_for(both_started.wait(), timeout=5)
        assert len(created) == 2
        for session_id, event in created.values():
            assert store._db.get_session(session_id)["ended_at"] is None
        release.set()
        await _drain_background_tasks(adapter)
        assert not adapter._serial_groups
        for session_id, event in created.values():
            row = store._db.get_session(session_id)
            assert row["ended_at"] is not None
            assert row["end_reason"] == "webhook_complete"
            assert runner._session_key_for_source(event.source) in runner.evicted_session_keys
        following = await deliver("instrument:A", "a3")
        assert following["status"] == "accepted"
        await _drain_background_tasks(adapter)
        assert not adapter._serial_groups
    assert store._db.prune_sessions(older_than_days=0, source="webhook") >= 3
    store._db.close()
