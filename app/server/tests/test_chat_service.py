from __future__ import annotations

import asyncio
import json

import pytest

from lynch_reader.chat.service import Conflict, Invalid, NotFound
from tests.chat_helpers import launches, make_service, next_event, state_of, until


def texts(snapshot):
    return [(i["kind"], i.get("text")) for i in snapshot["items"]]


def test_create_launches_with_session_id_and_replies(tmp_path):
    async def main():
        svc, log = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "hello", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return conv, snap, launches(log)
    conv, snap, runs = asyncio.run(main())
    assert texts(snap) == [("user", "hello"), ("text", "echo: hello")]
    argv = runs[0]["argv"]
    assert argv[argv.index("--session-id") + 1] == conv.id
    assert "ANTHROPIC_API_KEY" not in runs[0]["env"]
    assert snap["context"] == {"tokens": 1000, "window": 200000}
    assert conv.title == "hello" and conv.ticker == "AAA.US"


def test_second_message_reuses_the_process(tmp_path):
    async def main():
        svc, log = make_service(tmp_path)
        conv = await svc.create("research", None, "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        await svc.send(conv.id, "two")
        await until(lambda: len(svc.snapshot(conv.id)["items"]) == 4)
        await svc.shutdown()
        return launches(log)
    assert len(asyncio.run(main())) == 1


def test_dormant_conversation_resumes_after_restart(tmp_path):
    async def main():
        svc, log = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        await svc.shutdown()
        svc2, _ = make_service(tmp_path)  # same index and projects dir: a server restart
        assert state_of(svc2, conv.id) == "dormant"
        await svc2.send(conv.id, "two")
        await until(lambda: state_of(svc2, conv.id) == "idle")
        snap = svc2.snapshot(conv.id)
        await svc2.shutdown()
        return conv, snap, launches(log)
    conv, snap, runs = asyncio.run(main())
    argv = runs[1]["argv"]
    assert argv[argv.index("--resume") + 1] == conv.id and "--session-id" not in argv
    assert texts(snap)[-2:] == [("user", "two"), ("text", "echo: two")]


def test_interjection_while_running(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        await until(lambda: any(i["kind"] == "tool" for i in svc.snapshot(conv.id)["items"]))
        assert state_of(svc, conv.id) == "running"
        await svc.send(conv.id, "more")
        await until(lambda: state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    assert texts(asyncio.run(main()))[-2:] == [("user", "more"), ("text", "both handled")]


def test_interrupt_leaves_a_stopped_notice(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        await until(lambda: any(i["kind"] == "tool" for i in svc.snapshot(conv.id)["items"]))
        await svc.interrupt(conv.id)
        await until(lambda: state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    assert texts(asyncio.run(main()))[-1] == ("notice", "已停止")


def test_interrupt_on_a_process_that_just_died_is_a_conflict(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        await until(lambda: any(i["kind"] == "tool" for i in svc.snapshot(conv.id)["items"]))

        async def dead_pipe():  # the process exits between the alive check and the write
            raise BrokenPipeError
        svc._live[conv.id].proc.interrupt = dead_pipe
        try:
            with pytest.raises(Conflict):
                await svc.interrupt(conv.id)
        finally:
            await svc.shutdown()
    asyncio.run(main())


def test_crash_broadcasts_a_notice_and_goes_dormant(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "crash")
        notice = await next_event(q, "item", lambda d: d["kind"] == "notice")
        await until(lambda: state_of(svc, conv.id) == "dormant")
        await svc.shutdown()
        return notice
    assert "boom" in asyncio.run(main())["text"]


def test_start_failure_leaves_a_notice_and_dormant(tmp_path):
    # The process may die before or after our write lands in its pipe; either way the chat must end
    # dormant, never stuck "running", and the user must learn why (a 409 reason or a notice with stderr).
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        await svc.shutdown()
        q = svc.subscribe(conv.id)
        svc._env["FAKE_CLAUDE_FAIL_START"] = "1"
        refused = None
        try:
            await svc.send(conv.id, "two")
        except Conflict as e:
            refused = str(e)
        await until(lambda: state_of(svc, conv.id) == "dormant")
        await asyncio.sleep(0.2)  # let a late exit report arrive
        notices = []
        while not q.empty():
            event, data = q.get_nowait()
            if event == "item" and data["kind"] == "notice":
                notices.append(data["text"])
        return refused, notices, state_of(svc, conv.id)
    refused, notices, state = asyncio.run(main())
    assert state == "dormant"
    assert any("cannot resume session" in n for n in notices)
    assert refused is None or "Claude" in refused


def test_concurrent_sends_to_a_dormant_chat_share_one_process(tmp_path):
    async def main():
        svc, log = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        await svc.shutdown()
        await asyncio.gather(svc.send(conv.id, "two"), svc.send(conv.id, "three"))
        await until(lambda: ("text", "echo: three") in texts(svc.snapshot(conv.id)))
        await svc.shutdown()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return svc.snapshot(conv.id), launches(log), pending
    snap, runs, pending = asyncio.run(main())
    assert len(runs) == 2  # the create, then one resume
    assert ("text", "echo: two") in texts(snap) and ("text", "echo: three") in texts(snap)
    assert pending == []


def test_a_send_during_a_stop_keeps_its_new_process(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        stopping = asyncio.create_task(svc._stop(conv.id))
        await asyncio.sleep(0)  # the stop detaches the old process and waits for it to close
        await svc.send(conv.id, "slow")
        await stopping
        state = state_of(svc, conv.id)
        await until(lambda: any(i["kind"] == "tool" for i in svc.snapshot(conv.id)["items"]))
        await svc.interrupt(conv.id)
        await until(lambda: state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return state, snap
    state, snap = asyncio.run(main())
    assert state == "running"
    assert texts(snap)[-1] == ("notice", "已停止")


def test_a_stray_status_outside_a_turn_keeps_idle(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        svc._on_message(conv.id, svc._live[conv.id].proc, {"type": "system", "subtype": "status", "status": None})
        state = state_of(svc, conv.id)
        await svc.shutdown()
        return state
    assert asyncio.run(main()) == "idle"


def test_context_window_comes_from_the_conversation_model(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        svc._on_message(conv.id, svc._live[conv.id].proc, {"type": "result", "modelUsage": {
            "claude-haiku-4-5": {"contextWindow": 200000},
            "claude-opus-5-5[1m]": {"contextWindow": 1000000}}})
        window = svc.snapshot(conv.id)["context"]["window"]
        await svc.shutdown()
        return window
    assert asyncio.run(main()) == 1000000



def test_a_new_conversation_starts_with_the_window_last_seen_for_its_model(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        first = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, first.id) == "idle")
        same = await svc.create("ticker", "BBB.US", "two", "claude-opus-5-5", "medium")
        other = await svc.create("ticker", "CCC.US", "three", "claude-haiku-4-5", "low")
        await svc.shutdown()
        return same.context_window, other.context_window
    assert asyncio.run(main()) == (200000, None)


def test_rename_sets_the_title_and_tells_an_open_page(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "a long first question", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        q = svc.subscribe(conv.id)
        svc.rename(conv.id, "  AAA   估值  ")
        event = await next_event(q, "conv")
        with pytest.raises(Invalid):
            svc.rename(conv.id, "  ")
        with pytest.raises(Invalid):
            svc.rename(conv.id, ["x"])
        await svc.archive(conv.id)
        with pytest.raises(NotFound):
            svc.rename(conv.id, "late")
        await svc.shutdown()
        return event, svc._index.get(conv.id)
    event, stored = asyncio.run(main())
    assert event["title"] == "AAA 估值" and event["state"] == "idle"
    assert stored.title == "AAA 估值"


def test_a_new_conversation_takes_claudes_title_after_its_first_reply(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "what is priced in", "claude-opus-5-5", "medium")
        q = svc.subscribe(conv.id)
        event = await next_event(q, "conv", lambda d: d["title"] != "what is priced in")
        await svc.send(conv.id, "second")
        await until(lambda: len(svc.snapshot(conv.id)["items"]) == 4)
        title = svc._index.get(conv.id).title
        await svc.shutdown()
        return event, title
    event, title = asyncio.run(main())
    assert event["title"] == "title: what is priced in"
    assert title == "title: what is priced in"  # asked once: the second reply does not retitle


def test_a_rename_beats_a_title_still_on_its_way(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: svc._index.get(conv.id).title == "title: one")
        live = svc._live[conv.id]
        live.title_request = "title-late"  # as if a request were still out
        svc.rename(conv.id, "mine")
        svc._on_message(conv.id, live.proc, {"type": "control_response", "response": {
            "subtype": "success", "request_id": "title-late", "response": {"title": "late"}}})
        title = svc._index.get(conv.id).title
        await svc.shutdown()
        return title
    assert asyncio.run(main()) == "mine"


def test_a_rename_before_the_first_reply_ends_skips_the_title_request(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")  # a turn that waits
        await until(lambda: state_of(svc, conv.id) == "running")
        svc.rename(conv.id, "mine")
        await svc.interrupt(conv.id)  # the first reply ends here
        await until(lambda: state_of(svc, conv.id) == "idle")
        await asyncio.sleep(0.2)  # time for a title answer, had one been asked for
        live = svc._live[conv.id]
        asked = (live.title_from, live.title_request)
        title = svc._index.get(conv.id).title
        await svc.shutdown()
        return asked, title
    assert asyncio.run(main()) == ((None, None), "mine")

def test_deltas_stream_then_clear_on_the_full_text(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "hello")
        delta = await next_event(q, "delta")
        await next_event(q, "item", lambda d: d["kind"] == "text")
        await until(lambda: state_of(svc, conv.id) == "idle")
        partial = svc.snapshot(conv.id)["partial"]
        await svc.shutdown()
        return delta, partial
    delta, partial = asyncio.run(main())
    assert delta == {"text": "ech"} and partial == ""


def test_snapshot_mid_turn_shows_running_and_partial(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        await until(lambda: any(i["kind"] == "tool" for i in svc.snapshot(conv.id)["items"]))
        svc._live[conv.id].partial = "half a sen"  # as if a delta had arrived
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    snap = asyncio.run(main())
    assert snap["conv"]["state"] == "running" and snap["partial"] == "half a sen"
    assert texts(snap)[0] == ("user", "slow")


def test_unfinished_transcript_gets_a_notice_when_dormant(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "one", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "idle")
        await svc.shutdown()
        path = tmp_path / "projects" / f"{conv.id}.jsonl"
        last = json.loads(path.read_text().splitlines()[-1])["uuid"]
        with path.open("a") as f:  # a turn cut off by a server restart
            f.write(json.dumps({"type": "user", "uuid": "u-late", "parentUuid": last,
                                "message": {"role": "user", "content": "late"}}) + "\n")
        svc2, _ = make_service(tmp_path)
        return svc2.snapshot(conv.id)
    snap = asyncio.run(main())
    assert texts(snap)[-2:] == [("user", "late"), ("notice", "上次回答被中断")]


def test_list_counts_running_and_hides_missing_transcripts(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        busy = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        # Wait for the tool call, not just "running": only then has the fake written the transcript.
        await until(lambda: any(i["kind"] == "tool" for i in svc.snapshot(busy.id)["items"]))
        listing = svc.list()
        await svc.shutdown()
        return busy, listing
    busy, listing = asyncio.run(main())
    assert listing["running_count"] == 1 and [c["id"] for c in listing["chats"]] == [busy.id]
    (tmp_path / "projects" / f"{busy.id}.jsonl").unlink()  # still indexed, but Claude Code cleaned the transcript
    svc2, _ = make_service(tmp_path)
    assert svc2.list()["chats"] == []
    with pytest.raises(NotFound):
        svc2.snapshot(busy.id)


@pytest.mark.parametrize("kind,ticker,text,model,effort", [
    ("other", None, "x", "claude-opus-5-5", "medium"),
    ("ticker", None, "x", "claude-opus-5-5", "medium"),
    ("research", None, "  ", "claude-opus-5-5", "medium"),
    ("research", None, "x", "gpt-5", "medium"),
    ("research", None, "x", "claude-opus-5-5", "turbo"),
])
def test_create_validates(tmp_path, kind, ticker, text, model, effort):
    svc, _ = make_service(tmp_path)
    with pytest.raises(Invalid):
        asyncio.run(svc.create(kind, ticker, text, model, effort))


def test_options_lists_models_and_defaults(tmp_path):
    svc, _ = make_service(tmp_path)
    opts = svc.options()
    assert opts["default_model"] == "claude-opus-5-5" and opts["default_effort"] == "medium"
    assert {"id": "claude-haiku-4-5", "label": "Haiku 4.5"} in opts["models"]
