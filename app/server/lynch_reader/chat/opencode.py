"""How the app launches opencode for a 研究 / 讨论 chat: models, guardrails, environment, rules, tool names.

One `opencode acp` process per active chat (chat/opencode_link.py speaks ACP to it). It keeps the user's global
opencode config (providers, the oh-my-opencode-slim orchestrator preset) and only overlays, through
OPENCODE_CONFIG_CONTENT, the app's permissions and rules. Verified against opencode 1.18.25 with
oh-my-opencode-slim 2.2.8 on 2026-10-18; tests/test_chat_opencode_live.py guards it. Run it after every upgrade.

What loads (2026-10-18):
- off: ~/.claude/CLAUDE.md (OPENCODE_DISABLE_CLAUDE_CODE_PROMPT).
- on, no switch: the superpowers plugin (an overlay `plugin` list is merged, not replaced) and the repo-root
  AGENTS.md. Accepted: skills are text, and the bash denies below still hold.
- on, required: .claude/skills (sediment-framework). OPENCODE_DISABLE_CLAUDE_CODE and
  OPENCODE_DISABLE_CLAUDE_CODE_SKILLS would drop it, so neither is set.

Files are written only through the `edit` rules below; a plugin tool that writes on its own permission key must be
denied by name (oh-my-opencode-slim 2.2.8: ast_grep_replace; its acp_run exists only when acpAgents is configured,
which the user's setup does not do). Bash stays open, as in the Claude chats: a guardrail, not a sandbox.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lynch_reader.chat.config import (GIT_BLOCKED_COMMANDS, GIT_RULE_TAIL, LONGBRIDGE_BLOCKED_COMMANDS, SHARED_RULES,
                                      scope_line)
from lynch_reader.chat.index import Conversation

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
    opencode_cmd: tuple[str, ...]  # ("opencode",); tests put a fake script here
    repo_root: Path  # every process runs here; .mcp.json lives here
    logs: Path  # output/chat/opencode: <id>.jsonl (the app's transcript) and <id>.rules.md


def opencode_argv(cfg: OpencodeConfig) -> list[str]:
    return [*cfg.opencode_cmd, "acp"]


def permission_config(skill: str) -> dict:
    """opencode permissions from the same lists as the Claude chats (chat/config.py). Rules match in key order and
    the last match wins, so each catch-all comes first. `skill` is what an edit of the sediment-framework skill gets:
    "ask" (the phone approves it) or "deny"."""
    return {
        "bash": {
            "*": "allow",
            "sudo *": "deny",
            # The middle `*` also catches `git -C frameworks …`.
            **{f"git *{c}*": "deny" for c in GIT_BLOCKED_COMMANDS},
            **{f"longbridge {c}*": "deny" for c in LONGBRIDGE_BLOCKED_COMMANDS},
        },
        # Patterns are relative to the repo root; covers opencode's edit, write and patch tools.
        "edit": {"*": "deny", "frameworks/**": "allow", SKILL_PATTERN: skill},
        "external_directory": "deny",
        # oh-my-opencode-slim's AST rewrite writes files on disk without asking and is not an `edit` tool.
        "ast_grep_replace": "deny",
    }


def config_overlay(rules: Path) -> dict:
    """What goes on top of the user's global config. A subagent's ask never reaches the app (opencode does not
    forward a child session's permission requests over ACP; the turn would hang), so skill edits are denied to all
    agents and asked only for the orchestrator. Its agent-level rules also come after the user's own global bash
    asks, which would otherwise reach the app and be refused."""
    ask = permission_config("ask")
    return {
        "permission": permission_config("deny"),
        "agent": {OPENCODE_MODE: {"permission": {"bash": ask["bash"], "edit": ask["edit"]}}},
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
        return "Edit", {**pick(file_path="filePath", old_string="oldString", new_string="newString"),
                        "replace_all": bool(raw_input.get("replaceAll", False))}
    if name == "write":
        return "Write", pick(file_path="filePath", content="content")
    if name == "read":
        return "Read", pick(file_path="filePath", offset="offset", limit="limit")
    if name == "bash":
        return "Bash", pick(command="command")
    if name in ("grep", "glob"):
        return name.capitalize(), pick(pattern="pattern", path="path")
    if name == "webfetch":
        return "WebFetch", pick(url="url")
    if name.startswith("websearch"):  # the plugin's search MCP, e.g. websearch_web_search_exa
        return "WebSearch", pick(query="query")
    if name == "skill":
        return "Skill", pick(skill="name")
    if name == "task":  # a delegation to a subagent: Claude's Agent tool takes the same keys
        return "Agent", raw_input
    return name, raw_input


def mcp_servers(repo_root: Path) -> list[dict]:
    """The servers of the repo's .mcp.json (the Claude chats' MCP config) in ACP's session/new shape. Read at each
    launch and kept in memory only: the file holds credentials."""
    path = repo_root / ".mcp.json"
    if not path.exists():
        return []
    servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}

    def pairs(d: dict | None) -> list[dict]:
        return [{"name": k, "value": v} for k, v in (d or {}).items()]

    out = []
    for name, s in servers.items():
        if s.get("type") in ("http", "sse"):
            out.append({"type": s["type"], "name": name, "url": s["url"], "headers": pairs(s.get("headers"))})
        else:
            out.append({"name": name, "command": s["command"], "args": s.get("args", []), "env": pairs(s.get("env"))})
    return out
