"""Real HTTP admission/replay/cancellation acceptance for Harness #285."""

import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.platforms.webhook_lifecycle import WebhookLifecycle


@pytest.mark.asyncio
async def test_durable_lifecycle_response_loss_busy_finish_cancel_and_restart(tmp_path):
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "rate_limit": 3,
                "routes": {
                    "test": {
                        "secret": _INSECURE_NO_AUTH,
                        "prompt": "Review {id}",
                        "serial_key": "scope",
                        "serial_event_key": "id",
                    }
                },
            },
        )
    )
    adapter._lifecycle = WebhookLifecycle(tmp_path / "lifecycle.sqlite")
    gate = asyncio.Event()
    started = []
    starts = asyncio.Queue()
    finishes = asyncio.Queue()
    release = adapter._release_serial_group

    def released(chat):
        release(chat)
        finishes.put_nowait(chat)

    adapter._release_serial_group = released

    async def handler(event):
        started.append(event.source.chat_id)
        starts.put_nowait(event.source.chat_id)
        await gate.wait()
        return ""

    adapter._message_handler = handler
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:

        async def post(identity, scope="A"):
            r = await client.post(
                "/webhooks/test",
                json={"id": identity, "scope": scope},
                headers={"X-Request-ID": identity},
            )
            assert r.status in (200, 202), await r.text()
            return await r.json()

        async def control(identity, operation="status", scope="A"):
            r = await client.post(
                "/webhooks/test",
                json={
                    "operation": operation,
                    "deliveries": [{"identity": identity, "scope": scope}],
                },
            )
            assert r.status == 200, await r.text()
            return (await r.json())["deliveries"][0]

        await post("one")  # sender loses this response
        for _ in range(20):
            assert (await post("one"))["status"] == "accepted"
            assert (await control("one"))["status"] == "accepted"
        assert (await post("two"))["status"] == "busy"
        assert (await control("two"))["status"] == "unknown"
        assert (await control("two"))["scope_busy"]
        await asyncio.wait_for(starts.get(), 2)
        assert len(started) == 1
        assert len(adapter._rate_counts["test"]) == 1
        gate.set()
        await asyncio.wait_for(finishes.get(), 2)
        assert (await control("one"))["status"] == "completed"
        assert (await post("one"))["status"] == "completed"
        assert not (await control("two"))["scope_busy"]
        assert (await post("two"))["status"] == "accepted"
        await asyncio.wait_for(starts.get(), 2)
        await asyncio.wait_for(finishes.get(), 2)
        assert len(started) == 2
        gate.clear()
        assert (await post("active-cancel", "D"))["status"] == "accepted"
        await asyncio.wait_for(starts.get(), 2)
        assert len(started) == 3
        chat = started[-1]
        adapter._lifecycle.admit("test", "shared-risk", "D", chat)
        cancelled = await control("active-cancel", "cancel", "D")
        assert cancelled["status"] == "cancelled" and cancelled["consumer_running"]
        assert not gate.is_set() and finishes.empty()
        # Only the last obligation may stop a shared consumer.
        assert (await control("shared-risk", "cancel", "D"))["status"] == "cancelled"
        await asyncio.wait_for(finishes.get(), 2)
        assert not (await control("active-cancel", "status", "D"))["scope_busy"]
        assert (await post("active-cancel", "D"))["status"] == "cancelled"
        # A cancellation preceding a delayed POST persists across receiver restart.
        assert (await control("late", "cancel", "B"))["status"] == "cancelled"
        assert (await post("late", "B"))["status"] == "cancelled"
        adapter._lifecycle.admit("test", "crashed", "C", "old-chat")
        adapter._lifecycle.db.close()
        adapter._lifecycle = WebhookLifecycle(tmp_path / "lifecycle.sqlite")
        assert (await post("crashed", "C"))["status"] == "interrupted"
        assert (await post("late", "B"))["status"] == "cancelled"
        assert len(started) == 3
        adapter._routes["test"]["secret"] = "signed-only"
        denied = await client.post(
            "/webhooks/test",
            json={
                "operation": "cancel",
                "deliveries": [{"identity": "forged", "scope": "E"}],
            },
        )
        assert denied.status == 401
        assert adapter._lifecycle.get("test", "forged") is None
        assert adapter.sender_manages_resume("webhook:test:crashed")
        assert not adapter.sender_manages_resume("webhook:other:crashed")
        adapter._lifecycle.db.close()
