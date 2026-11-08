import asyncio

import pytest

from lynch_reader import web
from tests.chat_helpers import state_of, until
from tests.test_chat_codex_service import codex_service


@pytest.mark.parametrize("agent,model,effort", [
    ("claude", "claude-opus-5-5", "medium"),
    ("codex", "gpt-test", "low"),
    ("grok", "grok-4.6", "high"),
    ("opencode", "0g/deepseek-v4.1-flash", "default"),
])
def test_research_starts_even_when_project_updater_is_missing(tmp_path, monkeypatch, agent, model, effort):
    services = []

    def chat_service(*args, **kwargs):
        service = codex_service(tmp_path, **{k: kwargs[k] for k in ("skills",) if k in kwargs})
        services.append(service)
        return service

    monkeypatch.setattr(web, "REPO_ROOT", tmp_path)
    for name in ("FRAMEWORKS_DIR", "WEB_DIST", "INVEST_ROOT", "CANDIDATES_FILE", "WATCH_FILE", "DEVICES_FILE",
                 "ARCHIVE_FILE", "EVENTS_FILE", "IGNORED_FILE", "CALENDAR_FILE", "BASELINE_FILE"):
        monkeypatch.setattr(web, name, tmp_path / name)
    monkeypatch.setattr(web, "projects_dir_for", lambda repo, home: tmp_path / "projects")
    monkeypatch.setattr(web, "ChatService", chat_service)
    monkeypatch.setattr(web.uvicorn, "run", lambda *args, **kwargs: None)
    web.main([])

    async def main():
        service = services[0]
        try:
            conversation = await service.create("research", None, "hello", model, effort, agent=agent)
            await until(lambda: state_of(service, conversation.id) == "idle")
            await service.send(conversation.id, "continue")
            await until(lambda: state_of(service, conversation.id) == "idle")
            await service._stop(conversation.id)
            await service.send(conversation.id, "resume")
            await until(lambda: state_of(service, conversation.id) == "idle")
        finally:
            await service.shutdown()

    asyncio.run(main())
