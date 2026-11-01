from __future__ import annotations

import pytest


def test_models_keep_visible_gpt_capabilities_and_choose_valid_default():
    from lynch_reader.chat.codex import parse_models
    models = parse_models([
        {"model": "gpt-test", "displayName": "GPT Test", "isDefault": True,
         "defaultReasoningEffort": "high", "supportedReasoningEfforts": [
             {"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
        {"model": "gpt-other", "defaultReasoningEffort": "absent",
         "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]},
        {"model": "gpt-hidden", "hidden": True}, {"model": "other-provider/model"},
        {"model": "gpt-incomplete"}, None, "invalid",
    ])
    assert [m.public() for m in models] == [
        {"id": "gpt-test", "label": "GPT Test", "efforts": ["low", "high"], "default_effort": "high"},
        {"id": "gpt-other", "label": "gpt-other", "efforts": ["medium"], "default_effort": "medium"},
    ]
    assert models[0].is_default


@pytest.mark.parametrize("command,allowed", [
    ("longbridge quote AAA.US --format json", True), ("git -C frameworks status", True),
    ("python3 frameworks/_local/tmp/calc.py", True), ("echo sudo", True),
    ("sudo -n true", False), ("/usr/bin/sudo true", False),
    ("git -C frameworks push --dry-run", False), ("git reset --hard", False),
    ("git status && git -C frameworks checkout -- file", False),
    ("echo ok | longbridge order submit", False), ("longbridge --format json positions", False),
    ("env LANG=C longbridge auth login", False), ("bash -lc 'git -C frameworks clean -fd'", False),
    ("echo ok\ngit push", False),
    ("echo ok # status\nsudo -n true", False),
    ("pwd # workspace\nlongbridge positions", False),
    ("pwd # workspace\ngit -C frameworks push", False),
    ("echo '#' && sudo -n true", False),
    ("pwd # path \\\nsudo -n true", False),
    ('pwd # ignored "\nsudo -n true\n"', False),
    ("echo path#name; sudo -n true", False),
    ("echo '# quote' && git status", True),
    ("python3 - <<'PY'\nprint('# inside Python')\nPY", True),
    ('echo "$(sudo -n true)"', False),
    ("for x in a; do git -C frameworks push; done", False),
    ("bash -lc 'pwd # path \\\nsudo -n true'", False),
    ("bash -lc 'pwd # path \\\nsudo -n true'\"\"", False),
    ('bash -lc "pwd # path \\\nsudo -n true"', False),  # physical continuations are unsupported, fail closed
    ("su\\\ndo -n true", False),
    ("longbridge \\\npositions", False),
    ('bash -lc "pwd # path ' + ('\\' * 2) + '\nsudo -n true"', False),
    ('bash -lc "pwd # path ' + ('\\' * 4) + '\nsudo -n true"', False),
    ("bash -lc $'pwd # status\\nsudo -n true'", False),
    ("$'sudo' -n true", False),
    ('bash -lc "echo \\`sudo -n true\\`"', False),
    ('bash -lc "echo \\$(sudo -n true)"', False),
    ('bash -lc "echo $(printf \';sudo -n true\')"', False),
    ('env bash -lc "echo $(printf \';sudo -n true\')"', False),
    ('"$PROGRAM" -n true', False),
    ('bash -lc \'for x in a; do echo "$x"; done\'', True),
    ("bash --norc -c 'sudo -n true'", False),
    ('bash -lc "python3 -c \\"print(\'ok\')\\""', True),
    ("/usr/bin/g?t -C frameworks push", False),
    ("/usr/bin/[g]it -C frameworks push", False),
    ("/usr/bin/g{it,rep} -C frameworks push", False),
    ("'/usr/bin/g?t' -C frameworks status", True),
    ("env /usr/bin/g?t -C frameworks push", False),
    ("command /usr/bin/g?t -C frameworks push", False),
    ("exec /usr/bin/g?t -C frameworks push", False),
    ("nohup /usr/bin/g?t -C frameworks push", False),
    ("env '/usr/bin/g?t' -C frameworks status", True),
    ("exec -a other sudo -n true", False),
    ("env -S 'git push'", False),
    ("env -C . sudo -n true", False),
    ("VALUE='x /usr/bin/git -C frameworks'; env NOTE=$VALUE push", False),
    ("VALUE='x /usr/bin/git -C frameworks'; exec -a $VALUE push", False),
    ("exec -a /usr/bin/{echo,git} push", False),
    ("env 'NOTE=x /usr/bin/git -C frameworks' echo ok", True),
    ("exec -a 'x /usr/bin/git' echo ok", True),
    ("FLAGS=c; bash -$FLAGS sudo -n true", False),
    ("FLAGS=-c; env bash $FLAGS sudo -n true", False),
    ('echo bash -c "$DATA"', True),
])
def test_commands_apply_shared_deny_lists_to_shell_segments(command, allowed):
    from lynch_reader.chat.codex import command_allowed
    assert command_allowed(command) is allowed


def test_file_boundary_resolves_symlinks_and_requires_complete_valid_patch(tmp_path):
    from lynch_reader.chat.codex import file_scope
    root = tmp_path / "repo"
    frameworks = root / "frameworks"
    skill = frameworks / ".claude/skills/sediment-framework"
    skill.mkdir(parents=True)
    (frameworks / "outside").symlink_to(tmp_path, target_is_directory=True)
    (frameworks / "skill-link").symlink_to(skill, target_is_directory=True)
    assert file_scope(root, [{"path": "frameworks/AAA.US.md"}]) == "allow"
    assert file_scope(root, [{"path": str(skill / "probe.md")}]) == "ask"
    assert file_scope(root, [{"path": "frameworks/skill-link/probe.md"}]) == "ask"
    assert file_scope(root, [{"path": "frameworks/../AGENTS.md"}]) == "deny"
    assert file_scope(root, [{"path": "frameworks/outside/file"}]) == "deny"
    assert file_scope(root, [{"path": "frameworks/AAA.US.md"}, {"path": "app/server/x.py"}]) == "deny"
    assert file_scope(root, []) == "deny"
    assert file_scope(root, [{"path": "frameworks/AAA.US.md", "kind": {"type": "update", "move_path": "../x"}}]) == "deny"


def test_codex_prompt_reuses_framework_and_watch_rules(tmp_path):
    from lynch_reader.chat.codex import system_prompt
    from tests.chat_helpers import conv
    prompt = system_prompt(conv(agent="codex"))
    assert "frameworks/AAA.US.md" in prompt and "可以落盘" in prompt
    assert "lynch-watch" in prompt and "sediment-framework" in prompt
    assert "apply_patch" in prompt
