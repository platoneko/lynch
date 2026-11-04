"""Regression coverage for lifecycle rules shared by the two research ACP links."""

from __future__ import annotations

import asyncio

import pytest

from tests.chat_helpers import until
from tests.test_chat_grok_link import make_link as grok_link
from tests.test_chat_opencode_link import make_link as opencode_link


@pytest.mark.parametrize(
    "factory,hang",
    [
        (grok_link, "FAKE_GROK_ACP_HANG"),
        (opencode_link, "FAKE_OPENCODE_HANG"),
    ],
)
def test_cancelled_start_closes_the_process(tmp_path, factory, hang):
    async def main():
        link, _ = factory(tmp_path, env={hang: "1"})
        starting = asyncio.create_task(link.start())
        try:
            await until(lambda: link.alive and bool(link._calls))
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await starting
            assert not link.alive
            assert not link._calls
        finally:
            await link.close()

    asyncio.run(main())


def offline_wire(link):
    """Model the native protocol boundary, including accepted user identities and history reads."""
    messages = []

    async def request(method, path, body=None, **kwargs):
        assert method == "GET" and path.endswith("/message")
        return list(messages)

    if hasattr(link, "_http"):
        link._http.request = request

    async def write(obj):
        method = obj.get("method")
        if method == "_x.ai/rewind/points":
            link._on_response(obj["id"], {"result": {"rewind_points": []}})
        elif method == "session/prompt":
            text = obj["params"]["prompt"][0]["text"]
            index = len(messages)
            messages.append(
                {
                    "info": {"id": f"msg_{index}", "role": "user"},
                    "parts": [{"type": "text", "text": text}],
                }
            )
            if not hasattr(link, "_http"):
                link._on_update(
                    {
                        "sessionUpdate": "user_message_chunk",
                        "_meta": {"promptIndex": index},
                        "content": {"type": "text", "text": text},
                    }
                )

    link._proc.send_json = write
    link._session = "offline-session"


def test_grok_final_metadata_updates_usage_on_each_turn(tmp_path):
    async def main():
        link, events = grok_link(tmp_path)

        offline_wire(link)
        for count, (used, ticks) in enumerate(((123, 100000000), (456, 200000000)), 1):
            await link.prompt("question")
            link._on_update(
                {"sessionUpdate": "agent_message_chunk", "content": {"text": "reply"}}
            )
            link._on_response(
                link._prompt_id,
                {
                    "result": {
                        "stopReason": "end_turn",
                        "_meta": {
                            "totalTokens": used,
                            "usage": {"costUsdTicks": ticks},
                        },
                    }
                },
            )
            await until(lambda: len(events.turn_ends) == count)
        assert events.usage == [(123, 500000, 0.01), (456, 500000, 0.02)]
        replies = [e for e in events.entries if e["type"] == "assistant"]
        assert [e["message"]["usage"]["input_tokens"] for e in replies] == [123, 456]

    asyncio.run(main())


@pytest.mark.parametrize("factory", [grok_link, opencode_link])
def test_a_turn_without_usage_does_not_record_the_previous_turns_usage(
    tmp_path, factory
):
    async def main():
        link, events = factory(tmp_path)

        offline_wire(link)
        for count, used in enumerate((123, None), 1):
            await link.prompt("question")
            if used is not None:
                link._on_update(
                    {"sessionUpdate": "usage_update", "used": used, "size": 500000}
                )
            link._on_update(
                {"sessionUpdate": "agent_message_chunk", "content": {"text": "reply"}}
            )
            link._on_response(link._prompt_id, {"result": {"stopReason": "end_turn"}})
            await until(lambda: len(events.turn_ends) == count)
        replies = [e for e in events.entries if e["type"] == "assistant"]
        assert replies[0]["message"]["usage"] == {"input_tokens": 123}
        assert "usage" not in replies[1]["message"]

    asyncio.run(main())
