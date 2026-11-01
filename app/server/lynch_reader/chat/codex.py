"""Local Codex app-server configuration, model capabilities and research guardrails.

Uses the user's existing Codex provider/login without changing global configuration.
Protocol and approval policy checked against codex-cli 0.166.0.
"""
from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_bash

from lynch_reader.chat.approvals import APPROVABLE_SKILL
from lynch_reader.chat.config import GIT_BLOCKED_COMMANDS, GIT_RULE_TAIL, LONGBRIDGE_BLOCKED_COMMANDS, SHARED_RULES, scope_line
from lynch_reader.chat.index import Conversation

SETUP_TIMEOUT_S = 45.0
_BASH_LANGUAGE = Language(tree_sitter_bash.language())
_SHELLS = {"bash", "sh", "zsh", "dash"}
_DYNAMIC_WORDS = {"simple_expansion", "expansion", "command_substitution", "process_substitution",
                  "arithmetic_expansion", "brace_expression"}
_LITERAL_PROGRAM_NODES = {"command_name", "word", "string", "raw_string", "string_content", "concatenation",
                          "escape_sequence"}


@dataclass(frozen=True)
class CodexConfig:
    codex_cmd: tuple[str, ...]
    repo_root: Path
    logs: Path


@dataclass(frozen=True)
class CodexModel:
    id: str
    label: str
    efforts: tuple[str, ...]
    default_effort: str
    is_default: bool = False

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "efforts": list(self.efforts),
                "default_effort": self.default_effort}


def parse_models(rows: list[dict]) -> list[CodexModel]:
    out = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = row.get("model") or row.get("id")
        if not isinstance(mid, str) or not mid.startswith("gpt-") or row.get("hidden") or mid in seen:
            continue
        efforts = tuple(dict.fromkeys(e["reasoningEffort"] for e in row.get("supportedReasoningEfforts") or []
                                      if isinstance(e, dict) and isinstance(e.get("reasoningEffort"), str)))
        if not efforts:
            continue
        default = row.get("defaultReasoningEffort")
        out.append(CodexModel(mid, str(row.get("displayName") or mid), efforts,
                              default if default in efforts else efforts[0], bool(row.get("isDefault"))))
        seen.add(mid)
    return out


def codex_argv(cfg: CodexConfig) -> list[str]:
    return [*cfg.codex_cmd, "app-server", "--listen", "stdio://"]


def codex_env(base: Mapping[str, str]) -> dict[str, str]:
    return dict(base)


def system_prompt(conv: Conversation) -> str:
    return (f"{scope_line(conv)}\n{SHARED_RULES}\n"
            "- 写文件只用 apply_patch，且只写 frameworks/ 下；不通过 shell 绕过文件审批。\n"
            "- 修改 sediment-framework skill 要你本人操作，每次 patch 会把 diff 推到手机审批；"
            "子 agent 不要改这个 skill。其他不支持的审批会被拒绝。\n"
            f"- {GIT_RULE_TAIL}")


def command_allowed(command: object, _depth: int = 0) -> bool:
    """Apply the shared command deny lists to shell segments, including ordinary shell wrappers.

    As with the existing agents, this is a guardrail: arbitrary Python and scripts remain available.
    Unknown or malformed approval commands are refused rather than granted without a preview.
    """
    if _depth > 8 or not isinstance(command, (str, list)) or not command:
        return False
    if isinstance(command, list):
        return all(isinstance(a, str) for a in command) and _argv_allowed(command, _depth)
    if "\\\n" in command:
        return False  # do not emulate quote-dependent Bash continuation/escape expansion
    try:
        source = command.encode()
        root = Parser(_BASH_LANGUAGE).parse(source).root_node
        if root.has_error:
            return False
        nodes = [root]
        while nodes:
            node = nodes.pop()
            if node.type in ("ansi_c_string", "translated_string"):
                return False  # shlex cannot statically decode these Bash-specific quoting forms
            if node.type == "string" and not _supported_double_quotes(node.text):
                return False  # only escaped quote/backslash have the same meaning in Bash and shlex
            if node.type == "command":
                name = node.child_by_field_name("name")
                words = ([name] if name is not None else []) + node.children_by_field_name("argument")
                argv = []
                for word in words:
                    raw = word.text.decode()
                    parts = shlex.split(raw)  # only dequote a grammar-delimited POSIX argument
                    argv.append(parts[0] if len(parts) == 1 else raw)
                program = _program_index(argv)
                if (program is None
                        or any(_dynamic_word(word) for word in words[:program])
                        or (program < len(words) and _dynamic_word(words[program], program=True))):
                    return False  # preserve AST quoting after env/command/exec/nohup, before approving
                if (program < len(argv) and Path(argv[program]).name in _SHELLS
                        and any(_dynamic_word(word) for word in words[program + 1:])):
                    return False  # shell options can select code mode, so all startup arguments must be static
                if argv and not _argv_allowed(argv, _depth):
                    return False
            nodes.extend(node.named_children)  # includes commands in loops and command/process substitutions
        return bool(root.named_children)
    except (ValueError, UnicodeError, RecursionError):
        return False  # unknown shell syntax cannot be safely approved from a misleading preview


def _dynamic_word(node, *, program: bool = False) -> bool:
    program = program or node.type == "command_name"
    pending = [node]
    while pending:
        current = pending.pop()
        if (current.type in _DYNAMIC_WORDS
                or (program and current.type not in _LITERAL_PROGRAM_NODES)
                or (current.type == "word" and any(char in current.text for char in (b"*", b"?", b"[", b"{")))):
            return True
        pending.extend(current.named_children)
    return False


def _supported_double_quotes(text: bytes) -> bool:
    i = 0
    while i < len(text):
        if text[i] == 92:  # backslash: do not decode escaped $, backtick, newline, or unknown sequences
            if i + 1 >= len(text) or text[i + 1] not in (34, 92):
                return False
            i += 2
        else:
            i += 1
    return True


def _shell_c_flag(arg: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]


def _program_index(argv: list[str]) -> int | None:
    """One shared prefix decoder for AST checks and argv denies; unknown wrapper options fail closed."""
    i = 0
    while i < len(argv):
        while i < len(argv) and "=" in argv[i] and not argv[i].startswith("/"):
            i += 1
        if i == len(argv):
            return i
        name = Path(argv[i]).name
        if name not in ("env", "command", "exec", "nohup"):
            return i
        i += 1
        while i < len(argv) and (argv[i].startswith("-") or (name == "env" and "=" in argv[i])):
            flag = argv[i]
            i += 1
            if flag == "--":
                break
            if (name == "env" and flag in ("-u", "--unset")) or (name == "exec" and flag == "-a"):
                i += 1
            elif ((name == "env" and (flag in ("-i", "--ignore-environment") or not flag.startswith("-")))
                  or (name == "command" and flag in ("-p", "-v", "-V"))
                  or (name == "exec" and flag in ("-c", "-l"))):
                continue
            else:
                return None
        if i >= len(argv):
            return None
    return i


def _argv_allowed(argv: list[str], depth: int) -> bool:
    program = _program_index(argv)
    if program is None:
        return False
    argv = argv[program:]
    if not argv:
        return True
    name = Path(argv[0]).name
    if name in _SHELLS:
        for i, arg in enumerate(argv[1:], 1):
            if _shell_c_flag(arg):
                return i + 1 < len(argv) and command_allowed(argv[i + 1], depth + 1)
    if name == "sudo":
        return False
    if name == "git" and any(arg in GIT_BLOCKED_COMMANDS for arg in argv[1:]):
        return False
    if name == "longbridge" and any(arg.startswith(LONGBRIDGE_BLOCKED_COMMANDS) for arg in argv[1:]):
        return False
    return True


def file_scope(repo_root: Path, changes: object) -> str:
    """A whole native fileChange is allowed, needs skill approval, or is denied atomically."""
    if not isinstance(changes, list) or not changes:
        return "deny"
    framework_root = (repo_root / "frameworks").resolve()
    skill_root = (repo_root / APPROVABLE_SKILL).resolve()
    ask = False
    for change in changes:
        if not isinstance(change, dict):
            return "deny"
        paths = [change.get("path")]
        kind = change.get("kind") or {}
        if isinstance(kind, dict):
            move = kind.get("move_path") or kind.get("movePath")
            if move:
                paths.append(move)
        for raw in paths:
            if not isinstance(raw, str) or not raw:
                return "deny"
            try:
                path = Path(raw)
                path = (path if path.is_absolute() else repo_root / path).resolve()
            except (OSError, RuntimeError):
                return "deny"
            if not path.is_relative_to(framework_root):
                return "deny"
            ask |= path.is_relative_to(skill_root)
    return "ask" if ask else "allow"
