"""The service must derive lifecycle and write success from completed operations."""
from __future__ import annotations

import asyncio

import pytest

from lynch_reader.chat.service import Conflict, Live
from tests.chat_helpers import conv, make_service, state_of, until


@pytest.mark.parametrize("agent,model,effort,window", [
    ("grok", "grok-4.6", "high", 500000),
    ("opencode", "0g/deepseek-v4.1-flash", "default", None),
])
def test_completed_turn_during_send_drain_stays_idle(tmp_path, agent, model, effort, window):
    async def main():
        service, _ = make_service(tmp_path)
        try:
            chat = await service.create("research", None, "first", model, effort, window, agent=agent)
            await until(lambda: state_of(service, chat.id) == "idle")
            link = service._live[chat.id].proc
            if agent == "opencode":
                original_request = link._http.request

                async def drain_until_response(method, path, body=None, **kwargs):
                    result = await original_request(method, path, body, **kwargs)
                    if path.endswith("/prompt"):
                        await until(lambda: link._deferred_end is not None)
                    return result

                link._http.request = drain_until_response
            else:
                original_write = link._proc.send_json

                async def drain_until_response(obj):
                    await original_write(obj)
                    if obj.get("method") == "session/prompt":
                        await until(lambda: link._prompt_id is None)

                link._proc.send_json = drain_until_response
            await service.send(chat.id, "second")
            assert service.snapshot(chat.id)["items"][-1]["text"] == "echo: second"
            assert state_of(service, chat.id) == "idle"
            await service.send(chat.id, "third")
            assert state_of(service, chat.id) == "idle"
        finally:
            await service.shutdown()
    asyncio.run(main())


@pytest.mark.parametrize("agent,model,effort,window", [
    ("grok", "grok-4.6", "high", 500000),
    ("opencode", "0g/deepseek-v4.1-flash", "default", None),
])
def test_failed_send_leaves_the_chat_resumable(tmp_path, agent, model, effort, window):
    async def main():
        service, _ = make_service(tmp_path)
        try:
            chat = await service.create("research", None, "first", model, effort, window, agent=agent)
            await until(lambda: state_of(service, chat.id) == "idle")
            link = service._live[chat.id].proc

            async def broken(*_args, **_kwargs):
                raise BrokenPipeError("synthetic broken input")

            if agent == "opencode":
                link._http.request = broken
            else:
                link._proc.send_json = broken
            with pytest.raises(Conflict):
                await service.send(chat.id, "lost input")
            assert state_of(service, chat.id) == "dormant"
            await service.send(chat.id, "recovered")
            await until(lambda: state_of(service, chat.id) == "idle")
            assert service.snapshot(chat.id)["items"][-1]["text"] == "echo: recovered"
        finally:
            await service.shutdown()
    asyncio.run(main())


def test_claude_result_only_failure_is_visible(tmp_path):
    service, _ = make_service(tmp_path)
    chat = conv(kind="research", ticker=None)
    service._index.put(chat)
    live = Live(state="running")
    service._live[chat.id] = live
    queue = service.subscribe(chat.id)
    service._on_result(chat.id, live, {"type": "result", "subtype": "error_during_execution",
                                     "is_error": True, "errors": ["upstream unavailable"]})
    notices = [data for name, data in queue._queue if name == "item" and data["kind"] == "notice"]
    assert len(notices) == 1
    assert "upstream unavailable" in notices[0]["text"]
    assert live.state == "idle"


@pytest.mark.parametrize("outcome", ["pending", "failed", "denied", "success"])
def test_research_ticker_is_attached_only_after_a_successful_write(tmp_path, outcome):
    service, _ = make_service(tmp_path)
    chat = conv(kind="research", ticker=None)
    service._index.put(chat)
    live = Live(state="running")
    service._live[chat.id] = live
    service._show(chat.id, live, {"type": "assistant", "uuid": "a1", "message": {"content": [{
        "type": "tool_use", "id": "write1", "name": "Write",
        "input": {"file_path": str(tmp_path / "frameworks/AAA.US.md"), "content": "body"}}]}})
    assert service._index.get(chat.id).ticker is None
    if outcome != "pending":
        service._show(chat.id, live, {"type": "user", "uuid": "u1", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "write1", "is_error": outcome != "success",
            "content": "requires approval" if outcome == "denied" else outcome}]}})
    assert service._index.get(chat.id).ticker == ("AAA.US" if outcome == "success" else None)


@pytest.mark.parametrize("operation,destination,ticker", [
    ("delete", None, None),
    ("move", "frameworks/BBB.US.md", "BBB.US"),
    ("move", "frameworks/axes/archived.md", None),
])
def test_native_framework_changes_attach_only_a_surviving_framework(tmp_path, operation, destination, ticker):
    service, _ = make_service(tmp_path)
    chat = conv(kind="research", ticker=None)
    service._index.put(chat)
    live = Live(state="running")
    service._live[chat.id] = live
    inputs = {"file_path": str(tmp_path / "frameworks/AAA.US.md"), "operation": operation}
    if destination is not None:
        inputs["move_path"] = str(tmp_path / destination)
    service._show(chat.id, live, {"type": "assistant", "uuid": "a1", "message": {"content": [{
        "type": "tool_use", "id": "change1", "name": "Edit", "input": inputs}]}})
    service._show(chat.id, live, {"type": "user", "uuid": "u1", "message": {"content": [{
        "type": "tool_result", "tool_use_id": "change1", "is_error": False, "content": "success"}]}})
    assert service._index.get(chat.id).ticker == ticker
