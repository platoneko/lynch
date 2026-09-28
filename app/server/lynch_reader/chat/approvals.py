"""Calls Claude Code asks the app to permit (`--permission-prompt-tool stdio`).

Only edits to the sediment-framework skill reach the phone: Claude Code refuses to write under `.claude/`
whatever the whitelist says, so each such edit is shown to the user as a diff and waits for their answer.
Everything else it asks about is outside the whitelist and is refused on the spot, as before the flag.
"""
from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass
from pathlib import Path

APPROVABLE_SKILL = Path("frameworks/.claude/skills/sediment-framework")
EDIT_TOOLS = ("Edit", "Write")
# Both contain "requires approval" (items.DENIAL_MARKERS), so the refused call still shows as 已拒绝.
AUTO_DENY_MESSAGE = ("This call requires approval, and the phone app grants approval only for edits to the "
                     "sediment-framework skill. Tell the user what was blocked; do not work around it.")
USER_DENY_MESSAGE = ("The user declined this edit on the phone (it requires approval). "
                     "Ask the user how to proceed.")


@dataclass(frozen=True)
class Approval:
    request_id: str
    tool_use_id: str
    tool: str
    path: str  # relative to the repo root
    diff: str
    tool_input: dict  # sent back unchanged as `updatedInput` on allow

    def public(self) -> dict:
        """What the phone sees; the raw input stays on the server."""
        out = asdict(self)
        del out["tool_input"]
        return out


def approvable_path(tool: str, tool_input: dict, repo_root: Path) -> Path | None:
    """The skill file this Edit / Write would write, resolved through symlinks; None if it is anything else."""
    raw = tool_input.get("file_path")
    if tool not in EDIT_TOOLS or not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    # Resolving first: `.claude/skills` at the repo root is a symlink into frameworks/, and `..` or a symlink
    # inside the skill must not smuggle a write out of it.
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):  # a symlink loop is never approvable
        return None
    return resolved if resolved.is_relative_to((repo_root / APPROVABLE_SKILL).resolve()) else None


def edit_diff(tool: str, tool_input: dict, path: Path, display: str) -> str:
    """The change as a unified diff with 3 lines of context, computed against the file as it is now."""
    before = path.read_text(errors="replace") if path.is_file() else ""
    if tool == "Write":
        after = str(tool_input.get("content", ""))
    else:
        old, new = str(tool_input.get("old_string", "")), str(tool_input.get("new_string", ""))
        if not old or old not in before:  # Claude Code will fail this edit itself; still show what it tried
            return "\n".join(["（现文件里找不到要替换的原文，这次修改会失败）",
                              *(f"-{l}" for l in old.splitlines()), *(f"+{l}" for l in new.splitlines())])
        after = before.replace(old, new) if tool_input.get("replace_all") else before.replace(old, new, 1)
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{display}", f"b/{display}", lineterm="")
    return "\n".join(lines) or "（内容没有变化）"


def new_approval(request_id: str, request: dict, path: Path, repo_root: Path) -> Approval:
    # Relative to the resolved skill root, not the repo root: `frameworks/` may be a symlink out of the repo.
    display = (APPROVABLE_SKILL / path.relative_to((repo_root / APPROVABLE_SKILL).resolve())).as_posix()
    tool, tool_input = request["tool_name"], request.get("input") or {}
    return Approval(request_id=request_id, tool_use_id=str(request.get("tool_use_id", "")), tool=tool,
                    path=display, diff=edit_diff(tool, tool_input, path, display), tool_input=tool_input)
