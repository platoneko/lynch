from __future__ import annotations

import asyncio
import pytest

from lynch_reader.chat.service import Conflict, Live
from tests.chat_helpers import conv, make_service, until


class ControlledLink:
    """A protocol boundary whose completion can be delivered independently of send()."""

    alive = True

    def __init__(self, service, conversation):
        self.service, self.conversation = service, conversation
        self.calls = []
        self._log = service._app_log(conversation.id)
        self.events = service._link_events(conversation.id, lambda: self)

    async def prompt(self, text):
        self.calls.append(text)
        self.events.on_entry(self._log.user(text))

    def finish(self, reason="end_turn"):
        self.events.on_entry(
            self._log.message(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "answered " + self.calls[-1]}
                        ],
                    },
                }
            )
        )
        self.events.on_turn_end(reason, None)

    async def interrupt(self):
        self.events.on_turn_end("cancelled", None)

    async def close(self):
        self.alive = False

    async def rewind(self, message_uuid):
        self.rewound = message_uuid
        return "forked-session"

    async def generate_title(self, description):
        return "自动短标题"


def controlled(tmp_path, monkeypatch, agent):
    from tests.test_chat_codex_service import codex_service

    service = codex_service(tmp_path) if agent == "codex" else make_service(tmp_path)[0]
    conversation = conv(agent=agent)
    service._index.put(conversation)
    service._live[conversation.id] = Live(state="idle")
    link = ControlledLink(service, conversation)

    async def spawn(_conversation):
        service._live[conversation.id].proc = link
        service._set_state(conversation.id, "idle")

    for name in ("_spawn_opencode", "_spawn_research_grok", "_spawn_codex"):
        monkeypatch.setattr(service, name, spawn)
    return service, conversation, link


@pytest.mark.parametrize("agent", ["opencode", "grok", "codex"])
def test_busy_inputs_are_accepted_and_consumed_in_order(tmp_path, monkeypatch, agent):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        try:
            await service.send(conversation.id, "one", client_id="one")
            await service.send(conversation.id, "two", client_id="two")
            await service.send(conversation.id, "three", client_id="three")
            assert link.calls == ["one"]
            assert [row["text"] for row in service.snapshot(conversation.id)["queued"]][
                -2:
            ] == ["two", "three"]
            link.finish()
            await until(lambda: link.calls == ["one", "two"])
            link.finish()
            await until(lambda: link.calls == ["one", "two", "three"])
            link.finish()
            assert service.snapshot(conversation.id)["queued"] == []
            await service.send(conversation.id, "two", client_id="two")
            assert link.calls == ["one", "two", "three"]
        finally:
            await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize('agent', ['opencode', 'grok', 'codex'])
def test_native_idle_status_waits_for_terminal_response_before_next_send(tmp_path, monkeypatch, agent):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        try:
            await service.send(conversation.id, 'one')
            link.events.on_state('idle')  # another protocol channel can report idle before the final reply
            await service.send(conversation.id, 'two')
            assert link.calls == ['one']
            assert service.snapshot(conversation.id)['conv']['state'] == 'running'
            link.finish()
            await until(lambda: link.calls == ['one', 'two'])
            link.finish()
        finally:
            await service.shutdown()
    asyncio.run(main())


@pytest.mark.parametrize("agent", ["opencode", "grok", "codex"])
def test_stop_retains_queued_input_and_does_not_start_it(tmp_path, monkeypatch, agent):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        try:
            await service.send(conversation.id, "one")
            await service.send(conversation.id, "do this later", client_id="later")
            await service.interrupt(conversation.id)
            await asyncio.sleep(0)
            pending = service.snapshot(conversation.id)["queued"]
            assert link.calls == ["one"]
            assert (
                next(row for row in pending if row["id"] == "later")["status"]
                == "unanswered"
            )
            service.dismiss_queued(conversation.id, "later")
            assert all(
                row["id"] != "later"
                for row in service.snapshot(conversation.id)["queued"]
            )
        finally:
            await service.shutdown()

    asyncio.run(main())


def test_stop_does_not_leave_a_confirmed_native_input_marked_uncertain(tmp_path, monkeypatch):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, 'codex')
        try:
            await service.send(conversation.id, 'one', client_id='accepted')
            user = next(entry for entry in link._log.entries() if entry.get('client_id') == 'accepted')
            link._log.bind_native(user['uuid'], 'confirmed-native-turn')
            await service.interrupt(conversation.id)
            # Check the queue and its live event, without snapshot() doing reconnect reconciliation.
            assert service._queue.pending(conversation.id) == []
        finally:
            await service.shutdown()
    asyncio.run(main())


def test_send_waiting_for_chat_lock_cannot_be_accepted_after_shutdown(tmp_path, monkeypatch):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, 'opencode')
        live = service._live[conversation.id]
        await live.lock.acquire()
        pending = asyncio.create_task(service.send(conversation.id, 'must not be accepted'))
        await asyncio.sleep(0)
        await service.shutdown()
        live.lock.release()
        with pytest.raises(Conflict, match='已关闭'):
            await pending
        assert link.calls == []
        assert service._queue.pending(conversation.id) == []
    asyncio.run(main())


@pytest.mark.parametrize(
    "agent,field",
    [
        ("opencode", "opencode_session"),
        ("grok", "grok_session"),
        ("codex", "codex_thread"),
    ],
)
def test_edit_commits_native_fork_and_active_log_branch_together(
    tmp_path, monkeypatch, agent, field
):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        try:
            await service.send(conversation.id, "keep")
            link.finish()
            await service.send(conversation.id, "replace")
            link.finish()
            old = next(
                item
                for item in service.snapshot(conversation.id)["items"]
                if item.get("text") == "replace"
            )
            await service.edit(conversation.id, old["uuid"], "edited")
            assert link.rewound == old["uuid"]
            assert (
                getattr(service._index.get(conversation.id), field) == "forked-session"
            )
            assert link.calls == ["keep", "replace", "edited"]
            link.finish()
            texts = [
                item.get("text") for item in service.snapshot(conversation.id)["items"]
            ]
            assert "replace" not in texts and "answered replace" not in texts
            assert "keep" in texts and "edited" in texts
            assert any(
                entry.get("message", {}).get("content") == "replace"
                for entry in link._log.entries()
            )
        finally:
            await service.shutdown()

    asyncio.run(main())


def test_opencode_edit_commits_remapped_native_ids_with_branch(tmp_path, monkeypatch):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        try:
            await service.send(conversation.id, "keep")
            link.finish()
            first = next(e for e in link._log.entries() if e.get("message", {}).get("content") == "keep")
            link._log.bind_native(first["uuid"], "old-id")
            await service.send(conversation.id, "replace")
            link.finish()
            old = next(item for item in service.snapshot(conversation.id)["items"] if item.get("text") == "replace")
            link.fork_native_ids = {first["uuid"]: "fork-id"}
            await service.edit(conversation.id, old["uuid"], "edited")
            from lynch_reader.chat.app_log import AppLog
            assert AppLog(link._log.path).native_id(first["uuid"]) == "fork-id"
            branch = next(e for e in reversed(link._log.entries()) if e.get("subtype") == "lynch_branch")
            assert branch["native_ids"] == link.fork_native_ids
            assert branch["native_session"] == "forked-session"
        finally:
            await service.shutdown()
    asyncio.run(main())


def test_restart_reconciles_only_inputs_with_persisted_native_acceptance(
    tmp_path, monkeypatch
):
    service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
    for key in ("accepted", "unknown"):
        service._queue.add(conversation.id, key, key)
        service._queue.begin(conversation.id, key)
        link._log.client_id = key
        entry = link._log.user(key)
        if key == "accepted":
            link._log.bind_native(entry["uuid"], "msg_accepted")
    link._log.client_id = None
    restarted = make_service(tmp_path)[0]
    pending = restarted.snapshot(conversation.id)["queued"]
    assert [(row["id"], row["status"]) for row in pending] == [("unknown", "uncertain")]


def test_title_generation_waits_for_completion_and_queue_to_drain(
    tmp_path, monkeypatch
):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        live = service._live[conversation.id]
        live.title_from = "first"
        await service.send(conversation.id, "one")
        await service.send(conversation.id, "two")
        await service._request_title(conversation.id, live)
        assert service._index.get(conversation.id).title == conversation.title
        assert live.title_from == "first"
        service.rename(conversation.id, "用户手改")
        link.events.on_title("原生晚到标题")
        link.finish()
        await until(lambda: link.calls == ["one", "two"])
        link.finish()
        await asyncio.sleep(0)
        assert service._index.get(conversation.id).title == "用户手改"
        await service.shutdown()

    asyncio.run(main())


def test_discover_control_closes_before_queued_turn_starts(tmp_path, monkeypatch):
    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "grok")
        conversation = service._index.update(conversation.id, kind="discover")
        live = service._live[conversation.id]
        live.proc = link
        live.state = "compacting"
        service._queue.add(conversation.id, "next", "next")
        starts = []

        async def start(_conversation, _live, text):
            starts.append((text, link.alive))
            service._set_state(conversation.id, "running")

        monkeypatch.setattr(service, "_start_grok_turn", start)
        link.events.on_turn_end("end_turn", None)
        await until(lambda: starts)
        assert starts == [("next", False)]
        await service.shutdown()

    asyncio.run(main())


def test_terminal_send_error_clears_state_and_preserves_uncertain_input(
    tmp_path, monkeypatch
):
    from lynch_reader.chat.opencode_link import OpencodeSetupError
    from lynch_reader.chat.service import Conflict

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")

        async def broken(_text):
            raise OpencodeSetupError("wire failed")

        link.prompt = broken
        with pytest.raises(Conflict, match="wire failed"):
            await service.send(conversation.id, "keep original", client_id="uncertain")
        snap = service.snapshot(conversation.id)
        assert snap["conv"]["state"] == "dormant" and snap["partial"] == ""
        assert snap["queued"][0]["status"] == "uncertain"
        await service.shutdown()

    asyncio.run(main())


def test_native_fork_failure_preserves_app_history_and_session(tmp_path, monkeypatch):
    from lynch_reader.chat.service import Conflict

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        for text in ("keep", "replace"):
            await service.send(conversation.id, text)
            link.finish()
        before = link._log.entries()
        target = next(
            item
            for item in service.snapshot(conversation.id)["items"]
            if item.get("text") == "replace"
        )

        async def fail(_uuid):
            raise OSError("native fork failed")

        link.rewind = fail
        with pytest.raises(Conflict, match="native fork failed"):
            await service.edit(conversation.id, target["uuid"], "edited")
        assert link._log.entries() == before
        assert (
            service._index.get(conversation.id).opencode_session
            == conversation.opencode_session
        )
        await service.shutdown()

    asyncio.run(main())


def test_durable_branch_marker_repairs_stale_session_index(tmp_path, monkeypatch):
    service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
    parent = link._log.user("retained")
    link._log.branch_at(
        parent["uuid"], agent="opencode", native_session="ses_committed"
    )
    restarted = make_service(tmp_path)[0]
    assert (
        restarted.snapshot(conversation.id)["conv"]["opencode_session"]
        == "ses_committed"
    )


def test_closed_service_rejects_send_before_accepting_durable_input(
    tmp_path, monkeypatch
):
    from lynch_reader.chat.service import Conflict

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        link._log.user("existing")
        await service.shutdown()
        with pytest.raises(Conflict, match="关闭"):
            await service.send(
                conversation.id, "must not be accepted", client_id="after-close"
            )
        assert service._queue.pending(conversation.id) == []

    asyncio.run(main())


def test_discover_compact_send_failure_is_conflict_and_closes_control(
    tmp_path, monkeypatch
):
    from lynch_reader.chat.grok import GrokSetupError
    from lynch_reader.chat.service import Conflict

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "grok")
        conversation = service._index.update(conversation.id, kind="discover")

        async def spawn(_conversation, live):
            live.proc = link

        async def compact(_hint):
            raise GrokSetupError("native compact rejected")

        monkeypatch.setattr(service, "_spawn_discover_control", spawn)
        link.compact = compact
        with pytest.raises(Conflict, match="native compact rejected"):
            await service.send(conversation.id, "/compact")
        assert (
            not link.alive
            and service.snapshot(conversation.id)["conv"]["state"] == "dormant"
        )
        await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("fault", ["marker", "index"])
def test_edit_partial_commit_preserves_replacement_without_sending_to_wrong_branch(
    tmp_path, monkeypatch, fault
):
    from lynch_reader.chat.service import Conflict

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        for text in ("retained", "replace me"):
            await service.send(conversation.id, text)
            link.finish()
        target = next(
            item
            for item in service.snapshot(conversation.id)["items"]
            if item.get("text") == "replace me"
        )
        old_update = service._index.update
        if fault == "marker":

            def fail_marker(*args, **kwargs):
                raise OSError("marker write failed")

            monkeypatch.setattr(link._log, "branch_at", fail_marker)
        else:

            def fail_index(conv_id, **changes):
                if changes.get("opencode_session") == "forked-session":
                    raise OSError("index write failed")
                return old_update(conv_id, **changes)

            monkeypatch.setattr(service._index, "update", fail_index)
        with pytest.raises(Conflict, match="write failed"):
            await service.edit(
                conversation.id, target["uuid"], "preserve this replacement"
            )
        assert link.rewound == target["uuid"]
        assert link.calls == ["retained", "replace me"] and not link.alive
        pending = service._queue.pending(conversation.id)
        assert [(row["text"], row["status"]) for row in pending] == [
            ("preserve this replacement", "unanswered")
        ]
        monkeypatch.setattr(service._index, "update", old_update)
        restarted = make_service(tmp_path)[0]
        snap = restarted.snapshot(conversation.id)
        assert snap["queued"][0]["text"] == "preserve this replacement"
        expected = (
            "forked-session" if fault == "index" else conversation.opencode_session
        )
        assert snap["conv"]["opencode_session"] == expected
        assert not any(
            item.get("text") == "preserve this replacement" for item in snap["items"]
        )
        await service.shutdown()
        await restarted.shutdown()

    asyncio.run(main())
