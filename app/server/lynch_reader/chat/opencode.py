"""OpenCode 2 private-server configuration for research chats.

The user's providers and orchestrator plugin remain global; this overlay supplies the app's V2 permission
rules, shared research instructions and one-shot compaction hook. Shell access is a guardrail, not a sandbox.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lynch_reader.chat.config import (GIT_BLOCKED_COMMANDS, GIT_RULE_TAIL, LONGBRIDGE_BLOCKED_COMMANDS, SHARED_RULES,
                                      scope_line)
from lynch_reader.chat.index import Conversation
from lynch_reader.chat.mcp import load_mcp_servers

OPENCODE_MODELS: tuple[tuple[str, str], ...] = (("0g/deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),)
DEFAULT_OPENCODE_MODEL = "0g/deepseek-v4.1-flash"  # the user's choice (2026-10-18)
OPENCODE_EFFORT = "default"  # the 0g model has no effort variants
OPENCODE_MODE = "orchestrator"  # the user's choice: the plugin's orchestrator delegating to its subagents
SETUP_TIMEOUT_S = 30.0

SKILL_PATTERN = "frameworks/.claude/skills/sediment-framework/**"

_TOOL_RULES = f"""\
- 改 sediment-framework skill（frameworks/.claude/skills/sediment-framework/）和改框架一样，要用户明确同意；\
同意后，每次用 edit 或 write 改都会把 diff 推到用户手机上，等用户答复。skill 只能你自己改：子 agent 改它一律被拒绝。\
其他需要审批的调用一律拒绝。
- 写文件只用 edit 和 write 工具，且只写 frameworks/ 下。{GIT_RULE_TAIL}"""


@dataclass(frozen=True)
class OpencodeConfig:
    opencode_cmd: tuple[str, ...]  # ("opencode2",); tests put a fake script here
    repo_root: Path  # every process runs here; .mcp.json lives here
    logs: Path  # output/chat/opencode: <id>.jsonl (the app's transcript) and <id>.rules.md


def opencode_argv(cfg: OpencodeConfig) -> list[str]:
    return [*cfg.opencode_cmd, "serve"]


def permission_config(skill: str) -> list[dict]:
    """V2 uses ordered action/resource rules; the last matching rule wins."""
    return [
        {"action": "*", "resource": "*", "effect": "allow"},
        {"action": "shell", "resource": "sudo *", "effect": "deny"},
        *({"action": "shell", "resource": f"git *{c}*", "effect": "deny"} for c in GIT_BLOCKED_COMMANDS),
        *({"action": "shell", "resource": f"longbridge {c}*", "effect": "deny"}
          for c in LONGBRIDGE_BLOCKED_COMMANDS),
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "edit", "resource": "frameworks/**", "effect": "allow"},
        {"action": "edit", "resource": SKILL_PATTERN, "effect": skill},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        {"action": "ast_grep_replace", "resource": "*", "effect": "deny"},
        {"action": "question", "resource": "*", "effect": "deny"},
    ]


def config_overlay(rules: Path) -> dict:
    """Every child denies skill edits; only the orchestrator may ask the phone to approve its diff."""
    return {
        "permissions": permission_config("deny"),
        "agents": {OPENCODE_MODE: {"permissions": permission_config("ask")}},
        "instructions": [str(rules)],
    }


def system_prompt(conv: Conversation) -> str:
    return f"{scope_line(conv)}\n{SHARED_RULES}\n{_TOOL_RULES}"


def write_rules(cfg: OpencodeConfig, conv: Conversation) -> Path:
    """The rules as a file for `instructions` (added to the orchestrator's own prompt, not replacing it);
    rewritten at every launch so a resumed chat gets the current rules."""
    path = cfg.logs / f"{conv.id}.rules.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(system_prompt(conv), encoding="utf-8")
    return path


def opencode_env(base: Mapping[str, str], rules: Path) -> dict[str, str]:
    return {**base, "OPENCODE_CONFIG_CONTENT": json.dumps(config_overlay(rules), ensure_ascii=False),
            "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1"}


def claude_tool(name: str, raw_input: dict) -> tuple[str, dict]:
    """An opencode tool call in the Claude Code shape the app's items, approvals and pages know; a tool without a
    Claude counterpart (e.g. an MCP tool) keeps its own name and input."""
    def pick(**keys: str) -> dict:  # Claude key → opencode key, only those present
        return {claude: raw_input[oc] for claude, oc in keys.items() if oc in raw_input}

    if name == "edit":
        return "Edit", {**pick(file_path="path", old_string="oldString", new_string="newString"),
                        "replace_all": bool(raw_input.get("replaceAll", False))}
    if name == "write":
        return "Write", pick(file_path="path", content="content")
    if name == "read":
        return "Read", pick(file_path="path", offset="offset", limit="limit")
    if name in ("bash", "shell"):
        return "Bash", pick(command="command")
    if name in ("grep", "glob"):
        return name.capitalize(), pick(pattern="pattern", path="path")
    if name == "webfetch":
        return "WebFetch", pick(url="url")
    if name.startswith("websearch"):  # the plugin's search MCP, e.g. websearch_web_search_exa
        return "WebSearch", pick(query="query")
    if name == "skill":
        return "Skill", pick(skill="id")
    if name == "subagent":
        return "Agent", pick(description="description", subagent_type="agent", prompt="prompt")
    return name, raw_input


def v2_mcp_servers(repo_root: Path) -> dict:
    """Translate repo MCP configuration to V2 without persisting its credentials."""
    servers = load_mcp_servers(repo_root)

    out = {}
    for name, s in servers.items():
        if s.get("type") in ("http", "sse"):
            out[name] = {"type": "remote", "url": s["url"], "headers": s.get("headers", {})}
        else:
            out[name] = {"type": "local", "command": [s["command"], *s.get("args", [])],
                         "environment": s.get("env", {})}
    return out


def mcp_servers(repo_root: Path) -> list[dict]:
    """The existing ACP translator remains shared with Grok; V2 uses v2_mcp_servers instead."""
    def pairs(values: dict | None) -> list[dict]:
        return [{"name": key, "value": value} for key, value in (values or {}).items()]

    out = []
    for name, server in load_mcp_servers(repo_root).items():
        if server.get("type") in ("http", "sse"):
            out.append({"type": server["type"], "name": name, "url": server["url"],
                        "headers": pairs(server.get("headers"))})
        else:
            out.append({"name": name, "command": server["command"], "args": server.get("args", []),
                        "env": pairs(server.get("env"))})
    return out
