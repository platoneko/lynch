"""How the app launches Grok for a discover chat: models, isolation, argv, environment and appended rules.

Flags and tool names were verified against grok 1.0.46 on 2026-10-02 (spec §1); tests/test_chat_grok_live.py
guards them. Run it after every grok upgrade.
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

from lynch_reader.chat.index import Conversation
from lynch_reader.chat.kol import KolSources

DEFAULT_WINDOW = 256_000
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
DENY_RULES: tuple[str, ...] = ("Bash", "Edit", "Write", "Read", "Grep", "MCPTool(*)")


@dataclass(frozen=True)
class GrokConfig:
    grok_cmd: tuple[str, ...]  # ("grok",); tests put a fake script here
    workdir: Path  # every grok process runs here: an empty git root of its own
    home: Path  # holds ~/.grok/models_cache.json
    logs: Path  # the app's own discover transcripts (chat/grok_log.py)
    invest_root: Path  # where the KOL list comes from (chat/kol.py)


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


def grok_models(home: Path) -> list[GrokModel]:
    """Models that can search X, from grok's own catalog cache (`grok models` refreshes it); [] when unreadable."""
    try:
        catalog = json.loads((home / ".grok" / "models_cache.json").read_text(encoding="utf-8"))["models"]
        return [m for m in map(_model, catalog.values()) if m is not None]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def _model(entry: dict) -> GrokModel | None:
    info = entry["info"]
    if info.get("hidden") or not info.get("supports_backend_search"):
        return None
    windows = tuple(int(w) for w in info.get("context_windows") or [info["context_window"]])
    efforts = tuple(str(e["id"]) for e in info.get("reasoning_efforts") or [])
    if not efforts:
        return None
    default = info.get("reasoning_effort") if info.get("reasoning_effort") in efforts else efforts[0]
    return GrokModel(str(info["id"]), str(info.get("name") or info["id"]), windows, efforts, default)


def grok_env(base: Mapping[str, str]) -> dict[str, str]:
    return {**base, **ISOLATION_ENV}


def prepare_workdir(workdir: Path) -> None:
    """grok reads project settings and AGENTS.md up to the git root; its own root keeps it out of lynch's."""
    if (workdir / ".git").exists():
        return
    workdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(workdir)], check=True)


def grok_argv(cfg: GrokConfig, conv: Conversation, text: str, rules: str) -> list[str]:
    """One turn: grok answers `text` in the conversation's session and exits after its `result` line."""
    return [
        *cfg.grok_cmd, "--no-auto-update",
        f"--single={text}",  # `=`: a message starting with "-" is the prompt, not a flag
        "--resume", conv.grok_session,
        "--model", conv.model, "--reasoning-effort", conv.effort,
        "--rules", rules,
        "--disallowed-tools", ",".join(DISALLOWED_TOOLS), "--no-subagents",
        "--permission-mode", "dontAsk",
        *(arg for rule in DENY_RULES for arg in ("--deny", rule)),
        "--max-turns", str(MAX_TURNS),
        "--output-format", "streaming-messages-json", "--include-partial-messages",
    ]


def discover_rules(kol: KolSources) -> str:
    """Appended to grok's own system prompt (`--rules`); built every turn so a changed KOL list applies at once."""
    sources = []
    if kol.list_id:
        sources.append(f"X 列表 {kol.list_id}（x.com/i/lists/{kol.list_id}，搜索时写 list:{kol.list_id}）")
    if kol.handles:
        sources.append("账号 " + "、".join(f"@{h}" for h in kol.handles))
    return "\n".join([
        "你在 lynch 手机 app 的「发现新票」对话里工作；用户在手机上逐轮回复。始终用简体中文回答。",
        f"任务：用 X 搜索工具查看下列 KOL 最近 {kol.lookback_hours} 小时的帖子，找出他们提到的股票，"
        f"尤其是新提出的观点。KOL 来源：{'；'.join(sources)}。",
        "搜索规则：",
        "- 只看主帖，跳过回复别人的帖子。",
        "- X 搜索每次最多 10 条，要翻页，直到没有新帖或已超出回看窗口。",
        "- 列表和逐个账号（from:<handle>）两路都要搜：只搜列表会漏帖。",
        "- 用户可以改回看时长、只看某几位 KOL、追问某条帖子；照做。",
        "- 你只有 X 搜索和网页搜索；没有文件和终端工具，不要尝试。",
        "输出：先讲发现。末尾附恰好一个候选块；没有候选就不附。候选块格式：",
        "```lynch-candidates",
        '{"candidates": [{"ticker": "0700.HK", "name": "腾讯控股", "sources": [{"handle": "someone", '
        '"stance": "bullish", "reason": "一句话中文理由", "quote": "帖子原文摘录，保留原语言", '
        '"url": "https://x.com/someone/status/123", "posted_at": "2026-10-01T08:00:00Z"}]}]}',
        "```",
        "- ticker 写成 代码.市场，市场是 US、HK、SH、SZ、SG 之一，例如 AXTI.US、0700.HK、600519.SH。",
        "- name 必填：港股和 A 股写中文简称，其他市场写公司英文名。",
        "- stance 是 bullish、bearish 或 neutral。",
        "- 同一只票被多位 KOL 提到，合并成一条，sources 里列多个出处。",
        "- 每个出处都要有原帖链接和发帖时间（UTC，ISO 8601）。",
    ])


ACP_TIMEOUT_S = 30.0
ACP_LINE_LIMIT = 16 * 1024 * 1024
ACP_EXIT_GRACE_S = 5.0


class GrokSetupError(Exception):
    pass


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
