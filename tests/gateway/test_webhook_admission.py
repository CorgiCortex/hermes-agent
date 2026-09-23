import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


@pytest.mark.asyncio
async def test_busy_and_duplicate_do_not_spend_new_work_quota():
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "rate_limit": 2,
                "routes": {
                    "test": {
                        "secret": _INSECURE_NO_AUTH,
                        "prompt": "Review",
                        "deliver": "log",
                        "serial_key": "scope",
                    }
                },
            },
        )
    )
    release = asyncio.Event()
    runs = []

    async def handler(event):
        runs.append(event.source.chat_id)
        await release.wait()
        return ""

    adapter._message_handler = handler
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:

        async def send(scope, key):
            return await client.post(
                "/webhooks/test", json={"scope": scope}, headers={"X-Request-ID": key}
            )

        assert (await send("A", "a")).status == 202
        for i in range(100):
            r = await send("A", f"retry-{i}")
            assert r.status == 202 and (await r.json())["status"] == "busy"
        assert (await send("B", "b")).status == 202
        r = await send("C", "c")
        assert r.status == 429 and r.headers["Retry-After"] == "60"
        assert "c" not in adapter._seen_deliveries
        release.set()
        await asyncio.gather(*tuple(adapter._background_tasks))
        await asyncio.sleep(0.1)
        r = await send("A", "a")
        assert r.status == 200 and (await r.json())["status"] == "duplicate"
        assert len(runs) == 2
        assert len(adapter._rate_counts["test"]) == 2


@pytest.mark.asyncio
async def test_urgent_event_steers_existing_turn_once():
    from types import SimpleNamespace
    from gateway.run import GatewayRunner

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "rate_limit": 1,
                "routes": {
                    "test": {
                        "secret": _INSECURE_NO_AUTH,
                        "prompt": "Read event {id}, urgent={urgent}",
                        "deliver": "log",
                        "serial_key": "scope",
                        "serial_event_key": "id",
                        "serial_steer_key": "urgent",
                    }
                },
            },
        )
    )
    messages = []
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    runner._busy_input_mode = "steer"
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: source.chat_id
    runner._peek_session_state = lambda key: SimpleNamespace(
        turn=SimpleNamespace(
            agent=SimpleNamespace(steer=lambda text: messages.append(text) or True)
        )
    )
    adapter.gateway_runner = runner
    adapter._message_handler = lambda event: None
    adapter._serial_groups[("test", "A")] = "active-chat"
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        for i in range(100):
            r = await client.post(
                "/webhooks/test",
                json={"scope": "A", "id": "risk-1", "urgent": True},
                headers={"X-Request-ID": str(i)},
            )
            body = await r.json()
            assert (
                r.status == 202
                and body["steered"]
                and body["active_chat_id"] == "active-chat"
            )
        assert messages == ["Read event risk-1, urgent=True"]
        assert adapter._rate_counts == {}
        assert not adapter._background_tasks
        runner._peek_session_state = lambda key: None
        r = await client.post(
            "/webhooks/test", json={"scope": "A", "id": "risk-2", "urgent": True}
        )
        assert not (await r.json())["steered"]
        assert "risk-2" not in adapter._steered_events["active-chat"]
