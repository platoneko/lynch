from pathlib import Path

import pytest

from lynch_reader.chat import config, codex, grok, opencode
from tests.chat_helpers import conv


@pytest.mark.parametrize('prompt', [config.system_prompt, codex.system_prompt, grok.research_prompt, opencode.system_prompt])
def test_every_research_agent_has_the_same_local_reuse_protocol(prompt):
    text = prompt(conv(kind='ticker', ticker='ALPHA.US'))
    assert 'eval.lib.research_cli' in text
    assert 'LYNCH_RESEARCH_DIR' in text
    assert 'discover' in text and 'find' in text and 'put' in text
    assert '报告期' in text and '子 agent' in text
    assert '同一份结果' in text


@pytest.mark.parametrize('make_env', [config.launch_env, codex.codex_env, grok.research_env])
def test_research_directory_is_preserved_without_a_default(make_env):
    assert make_env({'LYNCH_RESEARCH_DIR': '/research'})['LYNCH_RESEARCH_DIR'] == '/research'
    assert 'LYNCH_RESEARCH_DIR' not in make_env({})


def test_static_research_commands_pass_without_expanding_file_permissions():
    for command in ('python3 -m eval.lib.research_cli find --symbol ALPHA.US',
                    'python3 -m eval.lib.research_cli read abcd --from-line 1 --to-line 20',
                    'python3 -m eval.lib.research_cli discover --root /data/research --symbol ALPHA.US'):
        assert codex.command_allowed(command)
    assert 'Write' not in config.ALLOWED_TOOLS
    assert not codex.command_allowed('sudo python3 -m eval.lib.research_cli find')


def test_discover_grok_keeps_its_existing_isolation():
    assert 'Bash' in grok.DENY_RULES
    assert 'MCPTool(*)' in grok.DENY_RULES
