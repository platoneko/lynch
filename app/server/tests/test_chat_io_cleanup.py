import asyncio
import json
import os
from pathlib import Path

import pytest

from lynch_reader.chat.service import Live
from tests.chat_helpers import conv
from tests.test_chat_codex_service import codex_service


@pytest.mark.parametrize("action", ["interrupt", "archive", "shutdown"])
def test_queue_write_failure_does_not_skip_native_stop(tmp_path, monkeypatch, action):
    async def main():
        service = codex_service(tmp_path)
        conversation = conv(agent="codex", model="gpt-test", effort="low")
        service._index.put(conversation)
        service._live[conversation.id] = Live(state="idle")
        await service.send(conversation.id, "wait", "first")
        await service.send(conversation.id, "must remain unsent", "later")
        link = service._live[conversation.id].proc
        original_replace = Path.replace

        def fail_queue_replace(path, target):
            if Path(target).name == "queue.json":
                raise OSError("simulated queue storage failure")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_queue_replace)
        try:
            try:
                if action == "interrupt":
                    await service.interrupt(conversation.id)
                elif action == "archive":
                    await service.archive(conversation.id)
                else:
                    await service.shutdown()
            except OSError:
                pass  # Exposing persistence failure is fine; leaving native work running is not.
            requests = [
                json.loads(line)
                for line in (tmp_path / "codex-rpc.jsonl").read_text().splitlines()
            ]
            native_stopped = not link.alive or any(
                r.get("method") == "turn/interrupt" for r in requests
            )
            assert native_stopped, (
                f"{action} skipped native stop while process is alive={link.alive}"
            )
        finally:
            monkeypatch.setattr(Path, "replace", original_replace)
            await service.shutdown()

    asyncio.run(main())


def test_shutdown_closes_every_chat_despite_queue_write_failure(
    tmp_path, monkeypatch, caplog
):
    async def main():
        service = codex_service(tmp_path)
        links = []
        for number in range(2):
            conversation = conv(
                id=f"chat-{number}", agent="codex", model="gpt-test", effort="low"
            )
            service._index.put(conversation)
            service._live[conversation.id] = Live(state="idle")
            await service.send(conversation.id, "wait", f"first-{number}")
            await service.send(conversation.id, "later", f"later-{number}")
            links.append(service._live[conversation.id].proc)
        original = Path.replace

        def fail(path, target):
            if Path(target).name == "queue.json":
                raise OSError("simulated queue storage failure")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", fail)
        try:
            await service.shutdown()
            assert not any(link.alive for link in links)
            assert "simulated queue storage failure" in caplog.text
            for number in range(2):
                assert any(
                    row["text"] == "later" and row["status"] == "unanswered"
                    for row in service._queue.pending(f"chat-{number}")
                )
        finally:
            monkeypatch.setattr(Path, "replace", original)
            await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("action", ["interrupt", "archive"])
@pytest.mark.parametrize("repair", ["queue", "index"])
def test_stop_does_not_require_preliminary_bookkeeping_repair(
    tmp_path, monkeypatch, action, repair
):
    async def main():
        service = codex_service(tmp_path)
        conversation = conv(agent="codex", model="gpt-test", effort="low")
        service._index.put(conversation)
        service._live[conversation.id] = Live(state="idle")
        await service.send(conversation.id, "wait", "running")
        link = service._live[conversation.id].proc
        log = service._app_log(conversation.id)
        if repair == "queue":
            service._queue.add(conversation.id, "already accepted", "accepted")
            service._queue.begin(conversation.id, "accepted")
            service._queue.pause(conversation.id)
            entry = log.user("already accepted", client_id="accepted")
            log.bind_native(entry["uuid"], "native-accepted")
        else:
            parent = next(
                entry for entry in log.entries() if entry.get("type") == "user"
            )
            log.branch_at(
                parent["uuid"], agent="codex", native_session="repaired-thread"
            )
        original = Path.replace
        original_os_replace = os.replace

        def fail(path, target):
            if Path(target).name == (
                "queue.json" if repair == "queue" else "index.json"
            ):
                raise OSError("preliminary repair unavailable")
            return original(path, target)

        if repair == "index":

            def fail_index(path, target):
                if Path(target).name == "index.json":
                    raise OSError("preliminary repair unavailable")
                return original_os_replace(path, target)

            monkeypatch.setattr(os, "replace", fail_index)
        else:
            monkeypatch.setattr(Path, "replace", fail)
        try:
            try:
                await getattr(service, action)(conversation.id)
            except OSError:
                pass
            requests = [
                json.loads(line)
                for line in (tmp_path / "codex-rpc.jsonl").read_text().splitlines()
            ]
            assert not link.alive or any(
                row.get("method") == "turn/interrupt" for row in requests
            )
        finally:
            monkeypatch.setattr(Path, "replace", original)
            monkeypatch.setattr(os, "replace", original_os_replace)
            await service.shutdown()

    asyncio.run(main())


def test_confirmation_write_failure_preserves_protocol_progress_and_pauses_queue(
    tmp_path, monkeypatch, caplog
):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        await service.send(conversation.id, "first", "first")
        await service.send(conversation.id, "later", "later")
        original = Path.replace

        def fail(path, target):
            if Path(target).name == "queue.json":
                raise OSError("confirmation storage unavailable")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", fail)
        try:
            link.events.on_delta("real native progress")
            snap = service.snapshot(conversation.id)
            assert snap["partial"] == "real native progress"
            assert [(row["id"], row["status"]) for row in snap["queued"]] == [
                ("first", "uncertain"),
                ("later", "unanswered"),
            ]
            assert "confirmation storage unavailable" in caplog.text
            await service.interrupt(conversation.id)
            assert link.calls == ["first"]
        finally:
            monkeypatch.setattr(Path, "replace", original)
            await service.shutdown()

    asyncio.run(main())


def test_snapshot_uses_authoritative_branch_when_index_repair_cannot_save(
    tmp_path, monkeypatch, caplog
):
    from tests.test_chat_agent_parity import controlled

    service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
    parent = link._log.user("retained")
    link._log.branch_at(
        parent["uuid"], agent="opencode", native_session="authoritative-native"
    )
    original = os.replace

    def fail(path, target):
        if Path(target).name == "index.json":
            raise OSError("index repair unavailable")
        return original(path, target)

    monkeypatch.setattr(os, "replace", fail)
    snap = service.snapshot(conversation.id)
    assert snap["conv"]["opencode_session"] == "authoritative-native"
    assert "index repair unavailable" in caplog.text


def test_new_input_write_failure_is_not_reported_as_accepted(tmp_path, monkeypatch):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        link._log.user("existing")
        original = Path.replace

        def fail(path, target):
            if Path(target).name == "queue.json":
                raise OSError("new input storage unavailable")
            return original(path, target)

        monkeypatch.setattr(Path, "replace", fail)
        try:
            with pytest.raises(OSError, match="new input storage unavailable"):
                await service.send(conversation.id, "must not execute", "new")
            assert link.calls == [] and service._queue.pending(conversation.id) == []
        finally:
            monkeypatch.setattr(Path, "replace", original)
            await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize(
    "agent,field",
    [
        ("codex", "codex_thread"),
        ("grok", "grok_session"),
        ("opencode", "opencode_session"),
    ],
)
def test_committed_compaction_publishes_boundary_when_index_save_fails(
    tmp_path, monkeypatch, caplog, agent, field
):
    from tests.test_chat_agent_parity import controlled

    service, conversation, link = controlled(tmp_path, monkeypatch, agent)
    service._live[conversation.id].proc = link
    service._live[conversation.id].context_tokens = 1234
    link._log.user("original retained history")
    link._log.message(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "original answer"}],
            },
        }
    )
    before = link._log.entries()
    events = service.subscribe(conversation.id)
    boundary = link._log.compacted(
        "manual", 1234, native_session="native-committed-summary", agent=agent
    )
    link._session = "native-committed-summary"
    original = os.replace

    def fail_index(path, target):
        if Path(target).name == "index.json":
            raise OSError("compaction index publication unavailable")
        return original(path, target)

    monkeypatch.setattr(os, "replace", fail_index)
    link.events.on_entry(boundary)
    assert (
        getattr(service._index.get(conversation.id), field)
        == "native-committed-summary"
    )
    assert link._session == "native-committed-summary"
    snap = service.snapshot(conversation.id)
    assert snap["conv"][field] == "native-committed-summary"
    assert snap["context"]["tokens"] is None
    published = []
    while not events.empty():
        published.append(events.get_nowait())
    compact = [
        data
        for event, data in published
        if event == "item" and data.get("kind") == "compact"
    ]
    assert len(compact) == 1 and compact[0]["key"] == boundary["uuid"]
    assert any(
        event == "context" and data["tokens"] is None for event, data in published
    )
    assert link._log.entries()[: len(before)] == before
    assert "compaction index publication unavailable" in caplog.text
