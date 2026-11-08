"""How the app launches Grok: discover (search-only `grok -p`) and research/ticker (persistent `grok agent stdio`).

Discover flags and tool names were verified against grok 1.0.46 on 2026-10-02 (spec §1); tests/test_chat_grok_live.py
guards them. Research ACP (GROK_HOME permission overlay, tool ACP shape, `_meta.rules`) was verified against
grok 1.0.70 on 2026-10-31 (spike §§1–8); tests/test_chat_grok_research_live.py guards that. Run both after a grok
upgrade.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from lynch_reader.chat.config import GIT_BLOCKED_COMMANDS, GIT_RULE_TAIL, LONGBRIDGE_BLOCKED_COMMANDS, SHARED_RULES, scope_line
from lynch_reader.chat.index import Conversation

DEFAULT_GROK_MODEL = "grok-4.5"  # the user's choice for new discover chats (2026-10-03)
DEFAULT_RESEARCH_MODEL = "grok-4.6"  # the user's choice for new 研究 / 讨论 chats (2026-10-31)
DEFAULT_RESEARCH_EFFORT = "high"
DEFAULT_WINDOW = 500_000
MAX_TURNS = 40  # twice a full KOL search's turns (5 on 2026-10-02, grok 1.0.46), floor 40

# grok's compatibility scanners would load other agents' settings: lynch's AGENTS.md and the broad Bash allows in
# .claude/settings.local.json, and ~/.cursor/mcp.json, whose GitHub server carries a plaintext token. A server child
# also keeps no memory and never updates itself. One sandbox profile for the ACP setup and every resume: a session
# refuses to resume under another (grok docs 18-sandbox.md).
ISOLATION_ENV: dict[str, str] = {
    **{f"GROK_{scanner}_{part}_ENABLED": "0" for scanner in ("CLAUDE", "CURSOR")
       for part in ("AGENTS", "RULES", "SKILLS", "HOOKS", "MCPS")},
    "GROK_MEMORY": "0",
    "GROK_DISABLE_AUTOUPDATER": "1",
    "GROK_SANDBOX": "read-only",
}
# Built-ins a search-only agent must not have. Internal names: the terminal is `run_terminal_cmd` although init
# lists `run_terminal_command` (both kept). A `--tools` whitelist is no alternative: it also drops X search, and
# one unknown name in it brings back every tool.
DISALLOWED_TOOLS: tuple[str, ...] = (
    "run_terminal_cmd", "run_terminal_command", "read_file", "search_replace", "list_dir", "grep", "write",
    "todo_write", "monitor", "workflow", "enter_plan_mode", "exit_plan_mode", "ask_user_question", "send_feedback",
    "image_gen", "image_edit", "image_to_video", "reference_to_video", "kill_command_or_subagent",
    "get_command_or_subagent_output", "spawn_subagent", "scheduler_create", "scheduler_delete", "scheduler_list",
    "Agent",
)
# ~/.grok/config.toml sets always-approve and allows Bash(*), Write, Edit and Agent; only deny rules beat those
# allows. A denial by rule lets the turn go on; one by dontAsk's fallback cancels the whole turn.
# The user's own MCP servers still load and grok announces them in the conversation: neither a workdir
# .grok/config.toml with enabled = false nor GROK_CONFIG_PATH with disabled_mcp_servers stopped them (grok 1.0.46,
# 2026-10-03). MCPTool(*) is what keeps their tools uncallable; APP_CONTEXT tells grok not to try.
DENY_RULES: tuple[str, ...] = ("Bash", "Edit", "Write", "Read", "Grep", "MCPTool(*)")

# Research chats cannot use discover's CLI --deny/--permission-mode: `grok agent stdio` ignores those top-level
# flags (spike §2). A per-chat GROK_HOME config.toml overrides the user's always-approve without writing the repo
# `.grok/config.toml` or `~/.grok/config.toml`. Child skill edits do reach session/request_permission (spike §8).
SKILL_ASK = (
    "Edit(frameworks/.claude/skills/sediment-framework/**)",
    "Write(frameworks/.claude/skills/sediment-framework/**)",
    "Edit(**/sediment-framework/**)",
    "Write(**/sediment-framework/**)",
)
RESEARCH_ALLOW: tuple[str, ...] = (
    "Edit(frameworks/**)", "Write(frameworks/**)", "Bash(*)", "Read", "Grep", "Glob",
    "WebSearch", "WebFetch", "MCPTool(wisburg__*)",
)
RESEARCH_ASK: tuple[str, ...] = SKILL_ASK
RESEARCH_DENY: tuple[str, ...] = (
    "Write(eval/**)", "Edit(eval/**)", "Bash(sudo *)",
    *(f"Bash(git *{c}*)" for c in GIT_BLOCKED_COMMANDS),
    *(f"Bash(longbridge {c}*)" for c in LONGBRIDGE_BLOCKED_COMMANDS),
    "MCPTool(longbridge__*)", "MCPTool(ida__*)", "MCPTool(github-nekomoto__*)",
)
RESEARCH_ENV: dict[str, str] = {
    "GROK_CURSOR_MCPS_ENABLED": "0",  # Cursor GitHub MCP must not load (spike §1)
    "GROK_DISABLE_AUTOUPDATER": "1",
    "GROK_MEMORY": "0",
}
_RESEARCH_TOOL_RULES = f"""\
- 改 sediment-framework skill（frameworks/.claude/skills/sediment-framework/）和改框架一样，要用户明确同意；\
同意后，每次用 write 或 search_replace 改都会把 diff 推到用户手机上，等用户答复。其他需要审批的调用一律拒绝。
- 写文件只用 write 和 search_replace 工具，且只写 frameworks/ 下。{GIT_RULE_TAIL}
- 对话里提示已连接的 MCP 外部工具服务，仓库 .mcp.json 以外的一律调用不了，也不要尝试。"""


@dataclass(frozen=True)
class GrokConfig:
    grok_cmd: tuple[str, ...]  # ("grok",); tests put a fake script here
    workdir: Path  # every grok process runs here: an empty git root of its own
    home: Path  # holds ~/.grok/models_cache.json
    logs: Path  # app transcripts (discover and research grok jsonl) plus research rules / GROK_HOME
    invest_root: Path  # where the KOL list comes from (chat/kol.py)
    skill: Path  # .agents/skills/discover-kol/references/search.md, sent whole every turn (discover_prompt)


@dataclass(frozen=True)
class GrokModel:
    id: str
    label: str
    windows: tuple[int, ...]
    efforts: tuple[str, ...]
    default_effort: str

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "windows": list(self.windows), "efforts": list(self.efforts),
                "default_effort": self.default_effort}


def grok_models(home: Path, *, search_only: bool = True) -> list[GrokModel]:
    """Models from grok's catalog cache (`grok models` refreshes it); [] when unreadable.

    `search_only=True` (discover) keeps those that can search X; `False` (research) keeps every visible model
    that advertises reasoning efforts.
    """
    try:
        catalog = json.loads((home / ".grok" / "models_cache.json").read_text(encoding="utf-8"))["models"]
        return [m for m in (_model(v, search_only=search_only) for v in catalog.values()) if m is not None]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def _model(entry: dict, *, search_only: bool = True) -> GrokModel | None:
    info = entry["info"]
    if info.get("hidden"):
        return None
    if search_only and not info.get("supports_backend_search"):
        return None
    windows = tuple(int(w) for w in info.get("context_windows") or [info["context_window"]])
    efforts = tuple(str(e["id"]) for e in info.get("reasoning_efforts") or [])
    if not efforts:
        return None
    default = info.get("reasoning_effort") if info.get("reasoning_effort") in efforts else efforts[0]
    return GrokModel(str(info["id"]), str(info.get("name") or info["id"]), windows, efforts, default)


def grok_env(base: Mapping[str, str]) -> dict[str, str]:
    return {**base, **ISOLATION_ENV}


def research_env(base: Mapping[str, str], grok_home: Path | None = None) -> dict[str, str]:
    """Discover isolation is off: the chat must see AGENTS.md and sediment-framework. Cursor MCP stays off."""
    env = {**base, **RESEARCH_ENV}
    if grok_home is not None:
        env["GROK_HOME"] = str(grok_home)
    return env


def research_argv(cfg: GrokConfig) -> list[str]:
    """Persistent ACP. Permission rules are the GROK_HOME config, not CLI flags (spike §2)."""
    return [*cfg.grok_cmd, "--no-auto-update", "agent", "stdio"]


def research_prompt(conv: Conversation) -> str:
    return f"{scope_line(conv)}\n{SHARED_RULES}\n{_RESEARCH_TOOL_RULES}"


def write_research_rules(cfg: GrokConfig, conv: Conversation) -> Path:
    """Rewritten at every launch so a resumed chat gets the current rules; GrokLink puts this on session/new `_meta.rules`."""
    path = cfg.logs / f"{conv.id}.rules.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(research_prompt(conv), encoding="utf-8")
    return path


def write_research_home(cfg: GrokConfig, conv: Conversation, real_home: Path, *, skill_home: Path | None = None) -> Path:
    """A per-chat grok home so dontAsk / allow / ask / deny override the user's always-approve (spike §2)."""
    try:
        home = cfg.logs / f"{conv.id}.grok-home"
        home.mkdir(parents=True, exist_ok=True)
        source = (skill_home or real_home) / "skills"
        if source.exists() and not source.is_dir():
            raise GrokSetupError(f"Grok 用户技能路径 {source} 不是目录")
        home.joinpath("config.toml").write_text(_research_config_toml(source), encoding="utf-8")
        for name in ("auth.json", "models_cache.json"):
            src, dest = real_home / name, home / name
            if dest.is_symlink() and dest.resolve() == src.resolve():
                continue
            if dest.is_symlink() or dest.exists():
                raise GrokSetupError(f"Grok 对话目录中的 {name} 已存在，已保留原文件")
            if src.exists():
                dest.symlink_to(src)
    except (OSError, RuntimeError) as error:
        raise GrokSetupError(f"无法准备 Grok 对话目录：{error}") from error
    return home


def _research_config_toml(skill_source: Path) -> str:
    def block(key: str, rules: tuple[str, ...]) -> str:
        body = "".join(f'    "{r}",\n' for r in rules)
        return f"{key} = [\n{body}]"
    return "\n".join([
        "[ui]",
        'permission_mode = "dontAsk"',
        "",
        "[permission]",
        block("allow", RESEARCH_ALLOW),
        block("ask", RESEARCH_ASK),
        block("deny", RESEARCH_DENY),
        "",
        "[skills]",
        f"paths = [{json.dumps(str(skill_source), ensure_ascii=False)}]",
        "",
    ])


def grok_tool(name: str, raw_input: dict) -> tuple[str, dict]:
    """Grok ACP tool_call title/rawInput in the Claude shape the app's items and approvals know (spike §3)."""
    variant = str(raw_input.get("variant") or "")
    title = name.strip()
    head = title.split()[0].lower() if title else ""

    def pick(*keys: str) -> dict:
        return {k: raw_input[k] for k in keys if k in raw_input}

    if head == "write" or variant == "Write":
        return "Write", pick("file_path", "content")
    if head in ("search_replace", "edit") or variant == "SearchReplace":
        return "Edit", {**pick("file_path", "old_string", "new_string"),
                        "replace_all": bool(raw_input.get("replace_all", False))}
    if head in ("read_file", "read") or variant == "ReadFile":
        inp = pick("offset", "limit")
        path = raw_input.get("target_file") or raw_input.get("file_path")
        if path is not None:
            inp["file_path"] = path
        return "Read", inp
    if head in ("run_terminal_command", "bash") or variant == "Bash" or title.startswith("Execute "):
        return "Bash", pick("command")
    if head == "grep" or variant == "Grep":
        return "Grep", pick("pattern", "path", "glob")
    if title.lower().startswith("web search") or variant == "WebSearch":
        return "WebSearch", pick("query") or dict(raw_input)
    if head == "spawn_subagent" or variant == "Task":
        return "Agent", raw_input
    return name, raw_input


def prepare_workdir(workdir: Path) -> None:
    """grok reads project settings and AGENTS.md up to the git root; its own root keeps it out of lynch's."""
    if (workdir / ".git").exists():
        return
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(workdir)], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise GrokSetupError(f"git init {workdir} 失败：{e.stderr.strip()[-300:]}") from e
    except OSError as e:
        raise GrokSetupError(f"无法创建工作目录 {workdir}：{e}") from e


def grok_argv(cfg: GrokConfig, conv: Conversation, prompt: str) -> list[str]:
    """One turn: grok answers `prompt` in the conversation's session and exits after its `result` line."""
    return [
        *cfg.grok_cmd, "--no-auto-update",
        f"--single={prompt}",  # `=`: a message starting with "-" is the prompt, not a flag
        "--resume", conv.grok_session,
        "--model", conv.model, "--reasoning-effort", conv.effort,
        "--disallowed-tools", ",".join(DISALLOWED_TOOLS), "--no-subagents",
        "--permission-mode", "dontAsk",
        *(arg for rule in DENY_RULES for arg in ("--deny", rule)),
        "--max-turns", str(MAX_TURNS),
        "--output-format", "streaming-messages-json", "--include-partial-messages",
    ]


USER_HEADING = "## 用户这一轮说"
APP_CONTEXT = ("你在 lynch 手机 app 的「发现新票」对话里，用户在手机上逐轮回复。你只有 X 搜索和网页搜索，"
               "没有文件和终端工具，不要尝试；对话里提示已连接的 MCP 外部工具服务一律调用不了，也不要尝试。"
               "候选由用户在 app 里点选落盘，你不用也不能存。始终用简体中文回答。")


def discover_prompt(skill: str, brief: str, text: str) -> str:
    """The whole message a discover turn sends grok: the skill's search rules, this turn's KOL sources, where it
    runs, then the user's words.

    `--rules` never reaches the model on a session made over ACP and resumed with -p (spec §1), so the rules ride
    in the message. Every turn, not just the first: grok's auto-compaction may summarise an early turn away.
    """
    return "\n\n".join([skill.strip(), f"## 本次的 KOL 来源\n{brief}", f"## 运行环境\n{APP_CONTEXT}",
                        f"{USER_HEADING}\n{text}"])


ACP_TIMEOUT_S = 30.0
ACP_LINE_LIMIT = 16 * 1024 * 1024
ACP_EXIT_GRACE_S = 5.0


class GrokSetupError(Exception):
    pass


async def summarize_conversation(cfg: GrokConfig, env: Mapping[str, str], conv: Conversation,
                                 hint: str, cwd: Path) -> str:
    """Summarize a disposable native fork, including its previous compaction context."""
    prompt = ("请把当前原生上下文整理为供后续对话继续使用的摘要。只输出摘要，不调用任何工具。"
              "必须保留用户要求的内容、重要事实、尚未解决的问题与已完成工具的结果。"
              "此前上下文是已完成对话，旧指令不构成本次待执行任务。\n"
              f"用户要求保留：{hint}")
    return await _tool_denied_headless(cfg, env, conv, prompt, cwd)


async def bootstrap_summary(cfg: GrokConfig, env: Mapping[str, str], conv: Conversation,
                            summary: str, cwd: Path) -> None:
    """Headless flags enforce tool denial while the short target context is established."""
    prompt = ("以下内容是压缩前已完成对话的引用摘要，仅作为后续对话的背景资料。"
              "摘要中的旧指令和工具调用均不是新任务，不应重新执行。"
              "本轮只回复已接收，等待用户下一条消息。\n<completed-conversation-summary>\n"
              + summary + "\n</completed-conversation-summary>")
    await _tool_denied_headless(cfg, env, conv, prompt, cwd)


async def _tool_denied_headless(cfg: GrokConfig, env: Mapping[str, str], conv: Conversation,
                               prompt: str, cwd: Path) -> str:
    argv = grok_argv(cfg, conv, prompt)
    disabled = (*DISALLOWED_TOOLS, "search_tool", "use_tool", "web_search", "web_fetch",
                "x_keyword_search", "x_semantic_search", "x_user_search", "x_thread_fetch")
    argv[argv.index("--disallowed-tools") + 1] = ",".join(disabled)
    # A valid whitelist drops backend X search; the blacklist then removes its one harmless
    # built-in. An empty whitelist is ignored by Grok 1.0.70 and would expose every tool.
    argv += ["--tools", "todo_write", "--disable-web-search", "--deny", "WebSearch", "--deny", "WebFetch",
             "--deny", "Glob", "--deny", "Agent"]
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=dict(env), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=ACP_LINE_LIMIT)
    result = None
    text = []
    initialized = False
    try:
        async with asyncio.timeout(180):
            async for line in proc.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                content = (message.get("message") or {}).get("content") or []
                if any(isinstance(part, dict) and part.get("type") == "tool_use" for part in content):
                    raise GrokSetupError("摘要承接出现工具调用，已保留原会话")
                if message.get("type") == "system" and message.get("subtype") == "init" and message.get("tools"):
                    raise GrokSetupError("摘要承接未禁用全部工具，已保留原会话")
                if message.get("type") == "system" and message.get("subtype") == "init":
                    initialized = True
                if message.get("type") == "assistant":
                    text.extend(part.get("text", "") for part in content
                                if isinstance(part, dict) and part.get("type") == "text")
                if message.get("type") == "result":
                    result = message
            await proc.wait()
            if (not initialized or proc.returncode != 0 or result is None or result.get("subtype") != "success"
                    or result.get("is_error") or not "".join(text).strip()):
                raise GrokSetupError("Grok 新摘要上下文未建立成功，原会话已保留")
            return "".join(text).strip()
    finally:
        await _end(proc)


async def new_session(cfg: GrokConfig, env: Mapping[str, str], model: str, window: int, *,
                      timeout_s: float = ACP_TIMEOUT_S) -> str:
    """A fresh grok session on `model` with `window` tokens of context; returns grok's session id.

    Headless `-p` has no way to pick the context window; ACP (`grok agent stdio`) does, and grok keeps the choice
    in the session for every later `--resume` (spec §1). The handshake calls no model, so it costs nothing.
    """
    prepare_workdir(cfg.workdir)
    proc = await asyncio.create_subprocess_exec(
        *cfg.grok_cmd, "agent", "stdio", cwd=cfg.workdir, env=dict(env),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=ACP_LINE_LIMIT)
    try:
        return await asyncio.wait_for(_handshake(_Acp(proc), cfg.workdir, model, window), timeout_s)
    except TimeoutError as e:
        raise GrokSetupError(f"grok 在 {timeout_s:g} 秒内没有响应") from e
    finally:
        await _end(proc)


async def _handshake(acp: "_Acp", workdir: Path, model: str, window: int) -> str:
    # This client offers grok no files and no terminal.
    await acp.call("initialize", {"protocolVersion": 1, "clientCapabilities": {
        "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    session = (await acp.call("session/new", {"cwd": str(workdir), "mcpServers": []})).get("sessionId")
    if not isinstance(session, str):
        raise GrokSetupError("session/new 没有返回会话 id")
    await acp.call("session/set_model", {"sessionId": session, "modelId": model, "_meta": {"contextWindow": window}})
    await acp.call("session/close", {"sessionId": session})  # grok writes the session out before answering
    return session


class _Acp:
    """JSON-RPC 2.0 over grok's stdio, one call at a time."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._last_id = 0

    async def call(self, method: str, params: dict) -> dict:
        self._last_id += 1
        rid = self._last_id
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                err = (await self._proc.stderr.read()).decode(errors="replace").strip()
                raise GrokSetupError(f"grok 提前退出：{err[-300:] or '没有输出'}")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "method" in msg and "id" in msg:  # grok asking its client for something this client does not offer
                await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {"code": -32601, "message": "not supported"}})
                continue
            if msg.get("id") != rid:
                continue  # a notification
            if "error" in msg:
                raise GrokSetupError(f"{method}：{(msg['error'] or {}).get('message')}")
            return msg.get("result") or {}

    async def _send(self, obj: dict) -> None:
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()


async def _end(proc: asyncio.subprocess.Process) -> None:
    """Lets grok exit on end of input, then ends whatever it left in its process group (MCP servers)."""
    if not proc.stdin.is_closing():
        proc.stdin.close()
    with suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), ACP_EXIT_GRACE_S)
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()
