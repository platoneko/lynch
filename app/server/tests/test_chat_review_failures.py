import asyncio
import json
import tomllib

import pytest

from lynch_reader.chat.service import Conflict, Live
from tests.chat_helpers import conv
from tests.test_chat_codex_service import codex_service


def test_grok_research_uses_configured_user_skill_home(tmp_path):
    async def main():
        user_home = tmp_path / "custom-grok"
        (user_home / "skills").mkdir(parents=True)
        service = codex_service(tmp_path, env={"GROK_HOME": str(user_home)})
        conversation = conv(kind="research", ticker=None, agent="grok", model="grok-4.6", effort="high")
        service._index.put(conversation)
        try:
            await service._spawn_research_grok(conversation)
            home = tmp_path / "grok-logs/c1.grok-home"
            config = tomllib.loads((home / "config.toml").read_text())
            assert config["skills"]["paths"] == [str(user_home / "skills")]
        finally:
            await service.shutdown()

    asyncio.run(main())


def test_grok_research_preserves_unknown_legacy_skill_entry(tmp_path):
    async def main():
        service = codex_service(tmp_path)
        conversation = conv(kind="research", ticker=None, agent="grok", model="grok-4.6", effort="high")
        service._index.put(conversation)
        entry = tmp_path / "grok-logs/c1.grok-home/skills"
        entry.mkdir(parents=True)
        (entry / "keep").write_text("keep")
        try:
            await service._spawn_research_grok(conversation)
            assert (entry / "keep").read_text() == "keep"
            assert service._live[conversation.id].proc is not None
        finally:
            await service.shutdown()

    asyncio.run(main())


def test_codex_unacknowledged_native_send_stays_uncertain(tmp_path):
    async def main():
        service = codex_service(tmp_path)
        conversation = conv(agent="codex", model="gpt-test", effort="low")
        service._index.put(conversation)
        service._live[conversation.id] = Live(state="idle")
        await service._spawn_codex(conversation)
        link = service._live[conversation.id].proc
        link._rpc._timeout = 0.1
        try:
            # The existing fake stores the user message and deliberately omits the RPC acknowledgement.
            with pytest.raises(Conflict, match="没有响应"):
                await service.send(
                    conversation.id, "hang-turn", client_id="unacknowledged"
                )
            user = next(
                e
                for e in service._app_log(conversation.id).entries()
                if e.get("client_id") == "unacknowledged"
            )
            assert service._app_log(conversation.id).native_id(user["uuid"]) is None
            native = list((tmp_path / "codex-sessions").glob("*.json"))
            assert json.loads(native[0].read_text()) == ["hang-turn"]
            assert service.snapshot(conversation.id)["queued"] == [
                {"id": "unacknowledged", "text": "hang-turn", "status": "uncertain"}
            ]
        finally:
            await service.shutdown()

    asyncio.run(main())


def test_stop_during_queued_codex_start_does_not_send_after_stop(tmp_path, monkeypatch):
    async def main():
        service = codex_service(tmp_path)
        conversation = conv(agent="codex", model="gpt-test", effort="low")
        service._index.put(conversation)
        service._live[conversation.id] = Live(state="idle")
        original = service._spawn_codex
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_spawn(c):
            entered.set()
            await release.wait()
            await original(c)

        monkeypatch.setattr(service, "_spawn_codex", blocked_spawn)
        pending = asyncio.create_task(
            service.send(conversation.id, "wait", client_id="stopped")
        )
        try:
            await entered.wait()
            await service.interrupt(conversation.id)
            release.set()
            with pytest.raises(Conflict):
                await pending
            native = list((tmp_path / "codex-sessions").glob("*.json"))
            assert all(json.loads(path.read_text()) == [] for path in native)
            assert service._live[conversation.id].proc is None
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(main())


def test_shutdown_during_queued_codex_start_cannot_start_a_new_turn(
    tmp_path, monkeypatch
):
    async def main():
        service = codex_service(tmp_path)
        conversation = conv(agent="codex", model="gpt-test", effort="low")
        service._index.put(conversation)
        service._live[conversation.id] = Live(state="idle")
        original = service._spawn_codex
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_spawn(c):
            entered.set()
            await release.wait()
            await original(c)

        monkeypatch.setattr(service, "_spawn_codex", blocked_spawn)
        pending = asyncio.create_task(
            service.send(conversation.id, "wait", client_id="stopped")
        )
        try:
            await entered.wait()
            await service.shutdown()
            release.set()
            try:
                await pending
            except Conflict:
                pass
            assert service._live[conversation.id].proc is None
            native = list((tmp_path / "codex-sessions").glob("*.json"))
            assert all(json.loads(path.read_text()) == [] for path in native)
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("agent", ["codex", "grok", "opencode"])
@pytest.mark.parametrize("bound", [False, True])
def test_synthetic_failure_only_resolves_input_with_native_acceptance(
    tmp_path, monkeypatch, agent, bound
):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        await service.send(conversation.id, "original", client_id="client-original")
        user = next(
            entry
            for entry in link._log.entries()
            if entry.get("client_id") == "client-original"
        )
        if bound:
            link._log.bind_native(user["uuid"], "native-accepted")
        link.events.on_entry(
            link._log.message(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Local synthetic error"}],
                    },
                }
            )
        )
        link.events.on_turn_end(None, "request not acknowledged")
        pending = service.snapshot(conversation.id)["queued"]
        assert pending == (
            []
            if bound
            else [{"id": "client-original", "text": "original", "status": "uncertain"}]
        )
        await service.shutdown()

    asyncio.run(main())


def test_discover_failed_result_without_progress_keeps_uncertain_input(
    tmp_path, monkeypatch
):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "grok")
        service._index.update(conversation.id, kind="discover")
        live = service._live[conversation.id]
        live.proc = link
        live.state = "running"
        service._queue.add(conversation.id, "original", "original")
        service._queue.begin(conversation.id, "original")
        service._on_result(
            conversation.id,
            live,
            {"subtype": "error_api", "is_error": True, "result": "No acknowledgement"},
        )
        assert service.snapshot(conversation.id)["queued"][0]["status"] == "uncertain"
        await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("agent", ["claude", "codex", "grok", "opencode"])
@pytest.mark.parametrize("action", ["stop", "shutdown"])
def test_stopping_during_research_preparation_prevents_start(tmp_path, agent, action):
    models = {"claude": ("claude-opus-5-5", "medium"), "codex": ("gpt-test", "low"),
              "grok": ("grok-4.6", "high"), "opencode": ("0g/deepseek-v4.1-flash", "default")}

    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare():
        entered.set()
        await release.wait()

    async def main():
        service = codex_service(tmp_path)
        service._make_room = prepare
        task = asyncio.create_task(
            service.create(
                "research", None, "do not execute", *models[agent], agent=agent
            )
        )
        try:
            await entered.wait()
            conversation = service._index.all()[0]
            if action == "stop":
                await service.interrupt(conversation.id)
            else:
                await service.shutdown()
            release.set()
            with pytest.raises(Conflict):
                await task
            assert service._live[conversation.id].proc is None
            if agent != "claude":
                assert (
                    service.snapshot(conversation.id)["queued"][0]["status"]
                    == "uncertain"
                )
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("action", ["stop", "shutdown"])
def test_discover_prepare_cannot_start_after_cancellation(
    tmp_path, monkeypatch, action
):
    from tests.chat_helpers import make_service

    async def main():
        service, _ = make_service(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service._kol_sources
        calls = 0

        async def sources():
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                await release.wait()
            return await original()

        monkeypatch.setattr(service, "_kol_sources", sources)
        task = asyncio.create_task(
            service.create(
                "discover", None, "do not execute", "grok-4.6", "high", 500000
            )
        )
        try:
            await entered.wait()
            conversation = service._index.all()[0]
            if action == "stop":
                await service.interrupt(conversation.id)
            else:
                await service.shutdown()
            release.set()
            with pytest.raises(Conflict):
                await task
            assert service._live[conversation.id].proc is None
        finally:
            release.set()
            await service.shutdown()

    asyncio.run(main())


def test_stop_cancels_background_queue_before_native_preflight_sends(
    tmp_path, monkeypatch
):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "opencode")
        await service.send(conversation.id, "first", client_id="first")
        await service.send(conversation.id, "second", client_id="second")
        original = link.prompt
        entered, release = asyncio.Event(), asyncio.Event()

        async def preflight(text):
            entered.set()
            await release.wait()
            await original(text)

        link.prompt = preflight
        link.finish()
        await entered.wait()
        await service.interrupt(conversation.id)
        release.set()
        await asyncio.sleep(0.02)
        assert link.calls == ["first"]
        assert service.snapshot(conversation.id)["queued"][0]["status"] == "uncertain"
        await service.shutdown()

    asyncio.run(main())


def test_interrupt_before_codex_start_ack_keeps_uncertain_input(tmp_path):
    from tests.chat_helpers import until

    async def main():
        service = codex_service(tmp_path)
        conversation = conv(agent="codex", model="gpt-test", effort="low")
        service._index.put(conversation)
        service._live[conversation.id] = Live(state="idle")
        await service._spawn_codex(conversation)
        pending = asyncio.create_task(
            service.send(
                conversation.id, "hang-turn", client_id="interrupted-before-ack"
            )
        )

        def native_received():
            return any(
                json.loads(path.read_text()) == ["hang-turn"]
                for path in (tmp_path / "codex-sessions").glob("*.json")
            )

        try:
            await until(native_received)
            await service.interrupt(conversation.id)
            with pytest.raises(Conflict):
                await pending
            user = next(
                entry
                for entry in service._app_log(conversation.id).entries()
                if entry.get("client_id") == "interrupted-before-ack"
            )
            assert service._app_log(conversation.id).native_id(user["uuid"]) is None
            assert service.snapshot(conversation.id)["queued"] == [
                {
                    "id": "interrupted-before-ack",
                    "text": "hang-turn",
                    "status": "uncertain",
                }
            ]
        finally:
            await service.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("agent", ["codex", "grok", "opencode"])
@pytest.mark.parametrize("stop,error", [(None, ""), ("failed", None)])
@pytest.mark.parametrize("bound", [False, True])
def test_empty_error_and_failed_status_pause_queue_without_assuming_acceptance(
    tmp_path, monkeypatch, agent, stop, error, bound
):
    from tests.test_chat_agent_parity import controlled

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        await service.send(conversation.id, "original", client_id="original")
        await service.send(conversation.id, "later", client_id="later")
        if bound:
            user = next(
                entry
                for entry in link._log.entries()
                if entry.get("client_id") == "original"
            )
            link._log.bind_native(user["uuid"], "accepted-native")
        link.events.on_turn_end(stop, error)
        await asyncio.sleep(0)
        expected = [] if bound else [("original", "uncertain")]
        expected.append(("later", "unanswered"))
        assert [
            (row["id"], row["status"])
            for row in service.snapshot(conversation.id)["queued"]
        ] == expected
        assert link.calls == ["original"]
        await service.shutdown()

    asyncio.run(main())


def test_waiting_send_resumes_committed_edit_branch_after_replacement_failure(
    tmp_path, monkeypatch
):
    from lynch_reader.chat.grok_link import GrokSetupError
    from tests.test_chat_agent_parity import controlled, ControlledLink

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, "grok")
        service._index.update(conversation.id, grok_session="original-native")
        for text in ("first", "replace"):
            await service.send(conversation.id, text, client_id=text)
            link.finish()
        target = next(
            item
            for item in service.snapshot(conversation.id)["items"]
            if item.get("text") == "replace"
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def rewind(_uuid):
            entered.set()
            await release.wait()
            return "forked-native"

        async def failed_prompt(_text):
            raise GrokSetupError("replacement send failed after native fork committed")

        link.rewind = rewind
        link.prompt = failed_prompt
        spawned = []

        async def spawn(current):
            spawned.append(current.grok_session)
            new = ControlledLink(service, current)
            service._live[current.id].proc = new
            service._set_state(current.id, "idle")

        monkeypatch.setattr(service, "_spawn_research_grok", spawn)
        editing = asyncio.create_task(
            service.edit(conversation.id, target["uuid"], "edited", client_id="edited")
        )
        await entered.wait()
        sending = asyncio.create_task(
            service.send(conversation.id, "concurrent message", client_id="concurrent")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        try:
            with pytest.raises(Conflict):
                await editing
            await sending
            assert spawned == ["forked-native"]
            assert (
                service._live[conversation.id].proc.conversation.grok_session
                == service._conv(conversation.id).grok_session
                == "forked-native"
            )
        finally:
            release.set()
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
@pytest.mark.parametrize("command", ["send", "compact", "edit"])
def test_locked_commands_choose_current_authoritative_native_session(
    tmp_path, monkeypatch, agent, field, command
):
    from tests.test_chat_agent_parity import controlled, ControlledLink

    async def main():
        service, conversation, link = controlled(tmp_path, monkeypatch, agent)
        service._index.update(conversation.id, **{field: "original-native"})
        for text in ("first", "replace"):
            await service.send(conversation.id, text)
            link.finish()
        target = next(
            item
            for item in service.snapshot(conversation.id)["items"]
            if item.get("text") == "replace"
        )
        live = service._live[conversation.id]
        await live.lock.acquire()
        if command == "edit":
            pending = asyncio.create_task(
                service.edit(conversation.id, target["uuid"], "edited")
            )
        else:
            pending = asyncio.create_task(
                service.send(
                    conversation.id,
                    "/compact" if command == "compact" else "new message",
                )
            )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        parent = next(
            entry["uuid"]
            for entry in reversed(link._log.entries())
            if entry.get("type") == "assistant"
        )
        link._log.branch_at(parent, agent=agent, native_session="current-native")
        service._index.update(conversation.id, **{field: "current-native"})
        await link.close()
        live.proc = None
        spawned = []

        async def spawn(current):
            spawned.append(getattr(current, field))
            new = ControlledLink(service, current)

            async def compact(_hint):
                new.events.on_turn_end("end_turn", None)

            new.compact = compact
            live.proc = new
            service._set_state(conversation.id, "idle")

        for name in ("_spawn_codex", "_spawn_research_grok", "_spawn_opencode"):
            monkeypatch.setattr(service, name, spawn)
        live.lock.release()
        try:
            await pending
            assert spawned == ["current-native"]
        finally:
            await service.shutdown()

    asyncio.run(main())
