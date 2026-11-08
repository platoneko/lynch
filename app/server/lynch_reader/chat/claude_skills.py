"""Expose user skills to project-only Claude without importing user settings."""
from dataclasses import dataclass
from fnmatch import fnmatchcase
import logging
from pathlib import Path
import re
import tempfile

log = logging.getLogger(__name__)

class ClaudeSkillBridgeError(Exception):
    pass


@dataclass(frozen=True)
class ClaudeSkillBridge:
    path: Path
    protected: tuple[Path, ...]


def permission_pattern(path: Path) -> str:
    # Claude's permission patterns are globs. Replacing metacharacters with single-character
    # wildcards makes a deny rule broader; interpreting a literal '[' as a character class can miss it.
    return "".join("?" if c in "*?[](){},!\\ \t\n\r" else c for c in str(path))


def _intersects_frameworks(path: Path, frameworks: Path) -> bool:
    a, b = Path(permission_pattern(path)).parts, frameworks.parts
    return all(fnmatchcase(right, left) for left, right in zip(a, b))


_NAME = re.compile(r"^name\s*:\s*(?:\"([A-Za-z0-9_-]+)\"|'([A-Za-z0-9_-]+)'|([A-Za-z0-9_-]+))\s*(?:#.*)?$")


def _skill_name(entry: Path) -> str | None:
    lines = (entry / "SKILL.md").read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        return entry.name
    names = []
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            return names[0] if len(names) == 1 else (entry.name if not names else None)
        if re.match(r"^name\s*:", line):
            match = _NAME.fullmatch(line)
            if not match:
                return None
            names.append(next(value for value in match.groups() if value is not None))
    return None


def prepare_claude_skill_bridge(repo: Path, home: Path, *, claude_home: Path | None = None) -> ClaudeSkillBridge | None:
    skills: dict[str, Path] = {}
    protected: set[Path] = set()
    frameworks = (repo / "frameworks").resolve()
    try:
        for root in ((claude_home or home / ".claude") / "skills", home / ".agents/skills"):
            try:
                entries = sorted(root.iterdir())
            except FileNotFoundError:
                continue
            except OSError as error:
                log.warning("无法读取用户技能目录 %s：%s", root, error)
                continue
            try:
                resolved_root = root.resolve()
            except (OSError, RuntimeError) as error:
                log.warning("无法解析用户技能目录 %s：%s", root, error)
                continue
            if any(c in str(resolved_root) for c in "\r\n"):
                log.warning("跳过路径包含换行的用户技能目录 %s", root)
                continue
            if _intersects_frameworks(resolved_root, frameworks):
                continue
            protected.add(resolved_root)
            for entry in entries:
                try:
                    if not entry.is_dir() or not (entry / "SKILL.md").is_file():
                        continue
                    resolved = entry.resolve()
                    if any(c in str(resolved) for c in "\r\n"):
                        log.warning("跳过路径包含换行的用户技能 %s", entry)
                        continue
                    if _intersects_frameworks(resolved, frameworks):
                        continue
                    if not (entry / "SKILL.md").resolve().is_relative_to(resolved):
                        log.warning("跳过技能目录外的技能文件 %s", entry)
                        continue
                    name = _skill_name(entry)
                    if name is None:
                        log.warning("跳过无法确定名称的用户技能 %s", entry)
                        continue
                    if name in skills:
                        continue
                    skills[name] = resolved
                    protected.add(resolved)
                except (OSError, UnicodeError, RuntimeError) as error:
                    log.warning("跳过无法读取的用户技能 %s：%s", entry, error)
        if not skills:
            return None
        temporary = repo / "_local/tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        temporary = temporary.resolve()
        if any(c in str(temporary) for c in "\r\n"):
            log.warning("临时技能桥路径包含换行，已跳过用户技能")
            return None
        if not temporary.is_relative_to(repo.resolve()):
            raise ClaudeSkillBridgeError("临时技能桥路径位于仓库之外")
        bridge = Path(tempfile.mkdtemp(prefix="claude-skills-", dir=temporary))
        destination = bridge / ".claude/skills"
        destination.mkdir(parents=True)
        for name, path in skills.items():
            (destination / name).symlink_to(path, target_is_directory=True)
        return ClaudeSkillBridge(bridge.resolve(), tuple(sorted(protected)))
    except (OSError, RuntimeError) as error:
        raise ClaudeSkillBridgeError(f"无法准备用户技能桥：{error}") from error
