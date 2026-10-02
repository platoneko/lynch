"""How the app launches Grok for a discover chat: models, isolation, argv, environment and appended rules.

Flags and tool names were verified against grok 1.0.46 on 2026-10-02 (spec §1); tests/test_chat_grok_live.py
guards them. Run it after every grok upgrade.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lynch_reader.chat.index import Conversation
from lynch_reader.chat.kol import KolSources

DEFAULT_WINDOW = 256_000
MAX_TURNS = 60  # recalibrated against a real KOL search in Task 9

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
