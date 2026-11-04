import asyncio

import pytest

from lynch_reader.chat.grok_link import GrokSetupError
from tests.chat_helpers import make_service, until


def test_uncertain_native_log_abort_pauses_following_inputs_at_service_boundary(
    tmp_path, monkeypatch
):
    async def main():
        service, _ = make_service(tmp_path)
        conversation = await service.create(
            "research", None, "slow", "grok-4.6", "high", 500000, agent="grok"
        )
        link = service._live[conversation.id].proc
        real_refresh = link._log.refresh
        real_compacted = link._log.compacted
        try:
            await until(lambda: bool(link._tools))
            await service.send(conversation.id, "must remain unsent", "later")

            def uncertain_write(*args, **kwargs):
                real_compacted(*args, **kwargs)
                raise OSError("boundary write returned uncertain")

            def unreadable():
                raise OSError("log temporarily unreadable")

            monkeypatch.setattr(link._log, "compacted", uncertain_write)
            monkeypatch.setattr(link._log, "refresh", unreadable)
            with pytest.raises(GrokSetupError, match="无法核对"):
                link._on_update(
                    {"sessionUpdate": "auto_compact_completed", "tokens_before": 900}
                )
            await link._log_abort_task
            assert not link.alive and service._live[conversation.id].proc is None
            queued = service._queue.pending(conversation.id)
            assert (
                next(row for row in queued if row["id"] == "later")["status"]
                == "unanswered"
            )
            assert service._queue.next(conversation.id) is None
        finally:
            monkeypatch.setattr(link._log, "compacted", real_compacted)
            monkeypatch.setattr(link._log, "refresh", real_refresh)
            await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("agent", ["codex", "grok", "opencode"])
@pytest.mark.parametrize("bound", [False, True])
def test_expected_owned_connection_exit_pauses_queue_before_late_events(
    tmp_path, monkeypatch, agent, bound
):
    from tests.test_chat_agent_parity import controlled, ControlledLink

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        await service.send(conversation.id, "first", "first")
        await service.send(conversation.id, "must remain unsent", "later")
        if bound:
            user = next(
                entry
                for entry in link._log.entries()
                if entry.get("client_id") == "first"
            )
            link._log.bind_native(user["uuid"], "native-first")
        await link.close()
        service._on_exit(conversation.id, link, True, "")
        link.events.on_turn_end("end_turn", None)
        expected = [] if bound else [("first", "uncertain")]
        expected.append(("later", "unanswered"))
        assert [
            (row["id"], row["status"])
            for row in service._queue.pending(conversation.id)
        ] == expected
        resumed = []

        async def spawn(current):
            adapter = ControlledLink(service, current)
            resumed.append(adapter)
            service._live[current.id].proc = adapter
            service._set_state(current.id, "idle")

        for name in ("_spawn_codex", "_spawn_research_grok", "_spawn_opencode"):
            monkeypatch.setattr(service, name, spawn)
        try:
            await service.send(conversation.id, "fresh request", "fresh")
            assert resumed[0].calls == ["fresh request"]
        finally:
            await service.shutdown()

    asyncio.run(main())


def test_expected_discovery_control_abort_pauses_without_completed_result(
    tmp_path, monkeypatch
):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "grok")
        await service.send(conversation.id, "first", "first")
        await service.send(conversation.id, "later", "later")
        service._index.update(conversation.id, kind="discover")
        service._live[conversation.id].result_seen = False
        await link.close()
        service._on_exit(conversation.id, link, True, "")
        assert service._queue.next(conversation.id) is None
        assert (
            next(
                row
                for row in service._queue.pending(conversation.id)
                if row["id"] == "later"
            )["status"]
            == "unanswered"
        )
        await service.shutdown()

    asyncio.run(main())


def test_normal_discovery_result_exit_still_drains_queued_input(tmp_path, monkeypatch):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "grok")
        service._index.update(conversation.id, kind="discover")
        live = service._live[conversation.id]
        live.proc = link
        live.state = "running"
        service._queue.add(conversation.id, "later", "later")
        started = []

        async def start(current, current_live, text):
            started.append(text)
            service._set_state(current.id, "running")

        monkeypatch.setattr(service, "_start_grok_turn", start)
        service._on_result(
            conversation.id, live, {"type": "result", "subtype": "success"}
        )
        await link.close()
        service._on_exit(conversation.id, link, True, "")
        await until(lambda: started)
        assert started == ["later"]
        await service.shutdown()

    asyncio.run(main())
