import asyncio
from pathlib import Path

import pytest

from lynch_reader.chat.claude_skills import permission_pattern, prepare_claude_skill_bridge
from lynch_reader.chat.config import LaunchConfig, launch_argv
from tests.chat_helpers import conv, launches, make_service, state_of, until


def make_skill(root: Path, name: str, contents: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(contents)
    return directory


def test_bridge_prefers_claude_user_skill_and_links_agents_skills(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    claude = make_skill(home / ".claude/skills", "shared", "claude")
    make_skill(home / ".agents/skills", "shared", "agents")
    agents = make_skill(home / ".agents/skills", "extra", "agents extra")
    external = make_skill(home / "elsewhere", "linked", "linked")
    (home / ".agents/skills/linked").symlink_to(external, target_is_directory=True)
    bridge = prepare_claude_skill_bridge(repo, home)
    assert bridge is not None
    assert (bridge.path / ".claude/skills/shared").resolve() == claude
    assert (bridge.path / ".claude/skills/extra").resolve() == agents
    assert (bridge.path / ".claude/skills/linked").resolve() == external
    assert {claude.parent.resolve(), agents.parent.resolve(), external.resolve()} <= set(bridge.protected)
    assert bridge.path.is_relative_to(repo / "_local/tmp")
    assert (claude / "SKILL.md").read_text() == "claude"


def test_missing_or_non_skill_entries_do_not_create_bridge(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    (home / ".claude/skills/not-a-skill").mkdir(parents=True)
    (home / ".agents/skills").mkdir(parents=True)
    (home / ".agents/skills/file").write_text("untouched")
    assert prepare_claude_skill_bridge(repo, home) is None
    assert (home / ".agents/skills/file").read_text() == "untouched"


def test_skill_name_collision_prefers_claude_even_when_directory_names_differ(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    first = make_skill(home / ".claude/skills", "first", '---\nname: "shared-id"\n---\nclaude')
    make_skill(home / ".agents/skills", "second", "---\nname: shared-id # note\n---\nagents")
    bridge = prepare_claude_skill_bridge(repo, home)
    assert (bridge.path / ".claude/skills/shared-id").resolve() == first
    assert len(list((bridge.path / ".claude/skills").iterdir())) == 1


def test_skills_with_different_names_survive_matching_directory_names(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    first = make_skill(home / ".claude/skills", "same-dir", "---\nname: claude-one\n---\nclaude")
    second = make_skill(home / ".agents/skills", "same-dir", "---\nname: agents-one\n---\nagents")
    bridge = prepare_claude_skill_bridge(repo, home)
    assert (bridge.path / ".claude/skills/claude-one").resolve() == first
    assert (bridge.path / ".claude/skills/agents-one").resolve() == second


def test_explicit_claude_config_directory_replaces_default_user_source(tmp_path):
    home, repo, config = tmp_path / "home", tmp_path / "repo", tmp_path / "profile"
    make_skill(home / ".claude/skills", "default", "---\nname: default\n---\ndefault")
    expected = make_skill(config / "skills", "custom", "---\nname: custom\n---\ncustom")
    bridge = prepare_claude_skill_bridge(repo, home, claude_home=config)
    assert (bridge.path / ".claude/skills/custom").resolve() == expected
    assert not (bridge.path / ".claude/skills/default").exists()


def test_unsafe_skill_cannot_block_other_user_skills(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    good = make_skill(home / ".claude/skills", "good", "---\nname: good\n---\ncontents")
    external = make_skill(home / "bad[dir]", "odd", "---\nname: odd\n---\ncontents")
    (home / ".agents/skills").mkdir(parents=True)
    (home / ".agents/skills/odd").symlink_to(external, target_is_directory=True)
    bridge = prepare_claude_skill_bridge(repo, home)
    assert (bridge.path / ".claude/skills/good").resolve() == good


def test_skill_with_newline_path_is_skipped_without_blocking_other_skills(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    good = make_skill(home / ".claude/skills", "good", "---\nname: good\n---\ngood")
    bad = make_skill(home / "bad\npath", "odd", "---\nname: odd\n---\nodd")
    (home / ".agents/skills").mkdir(parents=True)
    (home / ".agents/skills/odd").symlink_to(bad, target_is_directory=True)
    bridge = prepare_claude_skill_bridge(repo, home)
    assert (bridge.path / ".claude/skills/good").resolve() == good
    assert not (bridge.path / ".claude/skills/odd").exists()


def test_invalid_skill_metadata_does_not_prevent_other_skills(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    make_skill(home / ".claude/skills", "bad", "---\nname: [not, a, name]\n---\nbad")
    good = make_skill(home / ".agents/skills", "good", "---\nname: good\n---\ngood")
    bridge = prepare_claude_skill_bridge(repo, home)
    assert (bridge.path / ".claude/skills/good").resolve() == good
    assert not (bridge.path / ".claude/skills/bad").exists()


def test_skill_file_link_outside_skill_directory_is_not_read(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    bad = home / ".claude/skills/bad"
    bad.mkdir(parents=True)
    sensitive = tmp_path / ".env"
    sensitive.write_text("private")
    (bad / "SKILL.md").symlink_to(sensitive)
    good = make_skill(home / ".agents/skills", "good", "---\nname: good\n---\ngood")
    bridge = prepare_claude_skill_bridge(repo, home)
    assert (bridge.path / ".claude/skills/good").resolve() == good
    assert not (bridge.path / ".claude/skills/bad").exists()


def test_project_framework_skill_link_does_not_block_existing_phone_approval(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    project_skill = make_skill(repo / "frameworks/.claude/skills", "sediment-framework", "---\nname: sediment-framework\n---\nproject")
    user_skills = home / ".claude/skills"
    user_skills.mkdir(parents=True)
    (user_skills / "sediment-framework").symlink_to(project_skill, target_is_directory=True)
    make_skill(home / ".agents/skills", "other", "---\nname: other\n---\nother")
    bridge = prepare_claude_skill_bridge(repo, home)
    assert not (bridge.path / ".claude/skills/sediment-framework").exists()
    assert not any(project_skill.is_relative_to(path) or path.is_relative_to(repo / "frameworks") for path in bridge.protected)


def test_user_skill_root_encompassing_frameworks_does_not_deny_frameworks(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    make_skill(repo / "frameworks/.claude/skills", "sediment-framework", "---\nname: sediment-framework\n---\nproject")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/skills").symlink_to(repo, target_is_directory=True)
    make_skill(home / ".agents/skills", "other", "---\nname: other\n---\nother")
    bridge = prepare_claude_skill_bridge(repo, home)
    assert not any((repo / "frameworks").is_relative_to(path) for path in bridge.protected)


def test_expanded_deny_glob_cannot_match_frameworks_directory(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "badXdirY"
    make_skill(repo / "frameworks/.claude/skills", "sediment-framework", "---\nname: sediment-framework\n---\nproject")
    external = tmp_path / "bad[dir]"
    external.mkdir()
    (external / "SKILL.md").write_text("---\nname: odd\n---\nexternal")
    (home / ".claude/skills").mkdir(parents=True)
    (home / ".claude/skills/odd").symlink_to(external, target_is_directory=True)
    make_skill(home / ".agents/skills", "other", "---\nname: other\n---\nother")
    bridge = prepare_claude_skill_bridge(repo, home)
    assert not (bridge.path / ".claude/skills/odd").exists()


def test_permission_patterns_use_broader_safe_globs_for_literal_special_characters(tmp_path):
    home, repo = tmp_path / "home with [chars]", tmp_path / "repo"
    make_skill(home / ".claude/skills", "one", "---\nname: one\n---\ncontents")
    bridge = prepare_claude_skill_bridge(repo, home)
    argv = launch_argv(LaunchConfig(("claude",), repo), conv(), resume=False, skill_bridge=bridge)
    assert any("home?with??chars?" in value and value.startswith("Write(") for value in argv)
    assert permission_pattern(home) in " ".join(argv)


@pytest.mark.parametrize("resume", [False, True])
def test_launch_keeps_project_settings_and_refuses_bridge_writes(tmp_path, resume):
    home, repo = tmp_path / "home", tmp_path / "repo"
    make_skill(home / ".agents/skills", "one", "one")
    bridge = prepare_claude_skill_bridge(repo, home)
    argv = launch_argv(LaunchConfig(("claude",), repo), conv(), resume=resume, skill_bridge=bridge)
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv[argv.index("--add-dir") + 1] == str(bridge.path)
    denies = argv[argv.index("--disallowedTools") + 1:argv.index("--allowedTools")]
    for path in (bridge.path, *bridge.protected):
        for tool in ("Write", "Edit"):
            assert f"{tool}(/{path}/**)" in denies


def test_each_claude_launch_and_resume_gets_a_private_bridge(tmp_path):
    async def main():
        skill = make_skill(tmp_path / "home/.agents/skills", "one", "one")
        service, log = make_service(tmp_path)
        try:
            conversation = await service.create("research", None, "one", "claude-opus-5-5", "medium", agent="claude")
            await until(lambda: state_of(service, conversation.id) == "idle")
            await service._stop(conversation.id)
            await service.send(conversation.id, "resume")
            await until(lambda: state_of(service, conversation.id) == "idle")
            first, second = [run["argv"] for run in launches(log)]
            first_bridge = Path(first[first.index("--add-dir") + 1])
            second_bridge = Path(second[second.index("--add-dir") + 1])
            assert first_bridge != second_bridge
            assert (first_bridge / ".claude/skills/one").resolve() == skill
            assert (second_bridge / ".claude/skills/one").resolve() == skill
            assert "--resume" in second
        finally:
            await service.shutdown()

    asyncio.run(main())


def test_claude_launch_uses_configured_skills_profile(tmp_path):
    async def main():
        config = tmp_path / "profile"
        skill = make_skill(config / "skills", "one", "---\nname: one\n---\ncontents")
        service, log = make_service(tmp_path, env={"CLAUDE_CONFIG_DIR": str(config)})
        try:
            conversation = await service.create("research", None, "one", "claude-opus-5-5", "medium", agent="claude")
            await until(lambda: state_of(service, conversation.id) == "idle")
            argv = launches(log)[0]["argv"]
            bridge = Path(argv[argv.index("--add-dir") + 1])
            assert (bridge / ".claude/skills/one").resolve() == skill
        finally:
            await service.shutdown()

    asyncio.run(main())
