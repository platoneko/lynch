from __future__ import annotations

import asyncio

import pytest

from lynch_reader.chat.service import Conflict, Invalid, NotFound
from tests.chat_helpers import launches, make_service, next_event, state_of, until


def texts(snap):
    return [(i["kind"], i.get("text")) for i in snap["items"]]


async def ready(svc, text="one", kind="ticker", ticker="AAA.US"):
    conv = await svc.create(kind, ticker, text, "claude-opus-5-5", "medium")
    await until(lambda: state_of(svc, conv.id) == "idle")
    return conv


def test_compact_refused_while_running_and_passed_through_when_idle(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, conv.id) == "running")
        with pytest.raises(Conflict):
            await svc.send(conv.id, "/compact keep numbers")
        await svc.interrupt(conv.id)
        await until(lambda: state_of(svc, conv.id) == "idle")
        await svc.send(conv.id, "/compact keep numbers")
        await until(lambda: any(i["kind"] == "compact" for i in svc.snapshot(conv.id)["items"]))
        await until(lambda: state_of(svc, conv.id) == "idle")
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return snap
    snap = asyncio.run(main())
    compact = next(i for i in snap["items"] if i["kind"] == "compact")
    assert (compact["pre_tokens"], compact["post_tokens"]) == (1000, 100)
    assert snap["context"]["tokens"] is None


def test_edit_truncates_and_resumes_at_the_previous_reply(tmp_path):
    async def main():
        svc, log = make_service(tmp_path)
        conv = await ready(svc, "one")
        await svc.send(conv.id, "two")
        await until(lambda: len(svc.snapshot(conv.id)["items"]) == 4)
        await until(lambda: state_of(svc, conv.id) == "idle")
        items = svc.snapshot(conv.id)["items"]
        q = svc.subscribe(conv.id)
        await svc.edit(conv.id, items[2]["uuid"], "three")
        await next_event(q, "reset")
        await until(lambda: texts(svc.snapshot(conv.id))[-1] == ("text", "echo: three"))
        snap = svc.snapshot(conv.id)
        await svc.shutdown()
        return items, snap, launches(log)
    before, snap, runs = asyncio.run(main())
    assert texts(snap) == [("user", "one"), ("text", "echo: one"), ("user", "three"), ("text", "echo: three")]
    argv = runs[-1]["argv"]
    reply_one = before[1]["key"].split("#")[0]
    assert argv[argv.index("--resume-session-at") + 1] == reply_one


def test_edit_rejections(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await ready(svc, "one")
        first = svc.snapshot(conv.id)["items"][0]["uuid"]
        with pytest.raises(Invalid):
            await svc.edit(conv.id, first, "x")  # nothing before the first message to cut at
        with pytest.raises(Invalid):
            await svc.edit(conv.id, "not-a-uuid", "x")
        await svc.send(conv.id, "slow")
        await until(lambda: state_of(svc, conv.id) == "running")
        with pytest.raises(Conflict):
            await svc.edit(conv.id, first, "x")
        await svc.shutdown()
    asyncio.run(main())


def test_archive_kills_running_and_idle_processes(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        idle = await ready(svc, "one")
        busy = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, busy.id) == "running")
        procs = [svc._live[idle.id].proc, svc._live[busy.id].proc]
        await svc.archive(idle.id)
        await svc.archive(busy.id)
        listing = svc.list()
        await svc.shutdown()
        return procs, listing
    procs, listing = asyncio.run(main())
    assert not any(p.alive for p in procs)
    assert listing["chats"] == []


def test_archive_ticker_archives_only_that_ticker(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        a1 = await ready(svc, "one", ticker="AAA.US")
        await ready(svc, "two", ticker="AAA.US")
        b = await ready(svc, "three", ticker="BBB.US")
        count = await svc.archive_ticker("AAA.US")
        ids = [c["id"] for c in svc.list()["chats"]]
        await svc.shutdown()
        return count, ids, b, a1
    count, ids, b, _ = asyncio.run(main())
    assert count == 2 and ids == [b.id]


def test_archived_conversation_is_gone(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await ready(svc)
        await svc.archive(conv.id)
        for call in (lambda: svc.send(conv.id, "hi"), lambda: svc.edit(conv.id, "u", "x"),
                     lambda: svc.interrupt(conv.id)):
            with pytest.raises(NotFound):
                await call()
        with pytest.raises(NotFound):
            svc.subscribe(conv.id)
        await svc.shutdown()
    asyncio.run(main())


def test_idle_processes_are_reaped_but_running_ones_are_not(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc, _ = make_service(tmp_path, monotonic=lambda: clock["t"], idle_timeout_s=100)
        idle = await ready(svc, "one")
        busy = await svc.create("ticker", "AAA.US", "slow", "claude-opus-5-5", "medium")
        await until(lambda: state_of(svc, busy.id) == "running")
        clock["t"] = 1000
        await svc.reap_idle()
        states = state_of(svc, idle.id), state_of(svc, busy.id)
        await svc.shutdown()
        return states
    assert asyncio.run(main()) == ("dormant", "running")


def test_process_cap_closes_the_least_recently_used_idle(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc, _ = make_service(tmp_path, monotonic=lambda: clock["t"], max_processes=2)
        old = await ready(svc, "one")
        clock["t"] = 10
        new = await ready(svc, "two")
        clock["t"] = 20
        third = await ready(svc, "three")
        states = [state_of(svc, c.id) for c in (old, new, third)]
        await svc.shutdown()
        return states
    assert asyncio.run(main()) == ["dormant", "idle", "idle"]


def test_overage_stops_the_turn_with_a_notice(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await ready(svc)
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "overage")
        notice = await next_event(q, "item", lambda d: d["kind"] == "notice" and "超额" in d["text"])
        await until(lambda: state_of(svc, conv.id) == "idle")
        await svc.shutdown()
        return notice, svc.usage
    notice, usage = asyncio.run(main())
    assert "已停止" in notice["text"] and usage["five_hour"] == 0.1


def test_rejected_limit_gives_a_reset_notice(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await ready(svc)
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "limit")
        notice = await next_event(q, "item", lambda d: d["kind"] == "notice")
        await svc.shutdown()
        return notice
    assert "额度已满" in asyncio.run(main())["text"]


def test_research_conversation_attaches_to_the_ticker_it_lands(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        conv = await ready(svc, "look at X", kind="research", ticker=None)
        q = svc.subscribe(conv.id)
        await svc.send(conv.id, "write frameworks/_TEMPLATE.md")
        await until(lambda: state_of(svc, conv.id) == "idle")
        assert svc.snapshot(conv.id)["conv"]["ticker"] is None
        await svc.send(conv.id, "write frameworks/NEW.US.md")
        attached = await next_event(q, "conv")
        await svc.send(conv.id, "write frameworks/OTHER.US.md")
        await until(lambda: state_of(svc, conv.id) == "idle")
        final = svc.snapshot(conv.id)["conv"]["ticker"]
        await svc.shutdown()
        return attached, final
    attached, final = asyncio.run(main())
    assert attached["ticker"] == "NEW.US" and final == "NEW.US"  # first landing wins


def test_reaping_skips_a_chat_whose_lock_is_held(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc, _ = make_service(tmp_path, monotonic=lambda: clock["t"], idle_timeout_s=100)
        conv = await ready(svc, "one")
        clock["t"] = 1000
        async with svc._live[conv.id].lock:  # a send or edit is mid-way on this chat
            await svc.reap_idle()
            state = state_of(svc, conv.id)
        await svc.shutdown()
        return state
    assert asyncio.run(main()) == "idle"


def test_process_cap_does_not_close_a_locked_idle_chat(tmp_path):
    clock = {"t": 0.0}

    async def main():
        svc, _ = make_service(tmp_path, monotonic=lambda: clock["t"], max_processes=2)
        old = await ready(svc, "one")
        clock["t"] = 10
        new = await ready(svc, "two")
        clock["t"] = 20
        async with svc._live[old.id].lock:
            third = await ready(svc, "three")
        states = [state_of(svc, c.id) for c in (old, new, third)]
        await svc.shutdown()
        return states
    assert asyncio.run(main()) == ["idle", "dormant", "idle"]
