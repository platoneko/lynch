#!/usr/bin/env python3
"""Install upstream skills in staging, then publish only project-owned links."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

SOURCE = "himself65/finance-skills"
AGENTS = {"claude-code": "Claude Code", "codex": "Codex", "grok": "Grok Build", "opencode": "OpenCode"}
# The installer requires Node >=22.20; keep its runtime local instead of changing the host's Node.
INSTALLER = ("npx", "--yes", "--package", "node@22.20.0", "--package", "skills@1.7.6", "skills")


class SkillsUpdateError(Exception):
    pass


def installer_env(repo_root: Path) -> dict[str, str]:
    keys = {"PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    temporary = repo_root / "_local/tmp"
    return {**{k: v for k, v in os.environ.items() if k in keys}, "TMPDIR": str(temporary),
            "npm_config_cache": str(temporary / "npm-cache"), "GIT_TERMINAL_PROMPT": "0", "DISABLE_TELEMETRY": "1"}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    raise SkillsUpdateError(f"技能元数据无效：{path.name}")


def _installation(stage: Path, stdout: str) -> tuple[list[str], str]:
    try:
        rows = json.loads(stdout)
        if not isinstance(rows, list) or not rows:
            raise ValueError
        names = [row["name"] for row in rows]
        if (len(set(names)) != len(names) or any(not isinstance(n, str) or not n or Path(n).name != n
                                               or n in (".", "..") for n in names)
                or any(row.get("status") != "installed" or row.get("source") != SOURCE
                       or set(row.get("agents", [])) != set(AGENTS.values()) for row in rows)):
            raise ValueError
        lock = _read_json(stage / "skills-lock.json")["skills"]
        if set(lock) != set(names) or any(lock[n].get("source") != SOURCE or not lock[n].get("computedHash")
                                        for n in names):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SkillsUpdateError("安装器未完整安装四种 agent 的全部技能") from None
    return sorted(names), _digest(stage, names)


def _digest(stage: Path, names: list[str]) -> str:
    canonical = stage / ".agents/skills"
    digest = hashlib.sha256()
    for name in sorted(names):
        skill = canonical / name
        if not (skill / "SKILL.md").is_file():
            raise SkillsUpdateError(f"技能缺少 SKILL.md：{name}")
        for path in sorted(skill.rglob("*")):
            if path.is_symlink():
                raise SkillsUpdateError(f"技能包含未验证的软链接：{name}")
            if path.is_file():
                digest.update(str(path.relative_to(canonical)).encode())
                digest.update(b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _link(path: Path, target: Path) -> None:
    if path.is_symlink() and Path(os.path.abspath(path.parent / path.readlink())) == target:
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.symlink_to(os.path.relpath(target, path.parent), target_is_directory=True)
    os.replace(temporary, path)


def _publish(repo: Path, stage: Path, names: list[str], version: str) -> None:
    state = repo / "output/finance-skills"
    versions, canonical = state / "versions", repo / ".agents/skills"
    current = state / "current"
    if not state.resolve().is_relative_to(repo.resolve()):
        raise SkillsUpdateError("技能发布目录存在外部路径冲突")
    if versions.is_symlink() or (versions.exists() and not versions.is_dir()):
        raise SkillsUpdateError("技能版本目录冲突，拒绝覆盖")
    if current.exists() or current.is_symlink():
        target = Path(os.path.abspath(current.parent / current.readlink())) if current.is_symlink() else None
        if (target is None or target.parent != versions or len(target.name) != 64
                or any(c not in "0123456789abcdef" for c in target.name)):
            raise SkillsUpdateError("当前技能版本入口冲突，拒绝覆盖")
    if not canonical.resolve().is_relative_to(repo.resolve()):
        raise SkillsUpdateError("共享技能目录存在外部路径冲突")
    # A stable target identifies our links even if publication was interrupted before switching current.
    def managed(path: Path) -> bool:
        return (path.is_symlink() and Path(os.path.abspath(path.parent / path.readlink())) ==
                current / ".agents/skills" / path.name)

    old_names = {path.name for path in canonical.iterdir() if managed(path)} if canonical.is_dir() else set()
    for name in set(names) | old_names:
        path = canonical / name
        if (path.exists() or path.is_symlink()) and not managed(path):
            raise SkillsUpdateError(f"技能入口冲突，拒绝覆盖：{name}")
    sediment = repo / "frameworks/.claude/skills/sediment-framework"
    own_skill = canonical / "sediment-framework"
    if not sediment.is_dir() or "sediment-framework" in names or "discover-kol" in names:
        raise SkillsUpdateError("项目自有技能缺失或与上游技能冲突")
    if (own_skill.exists() or own_skill.is_symlink()) and own_skill.resolve() != sediment.resolve():
        raise SkillsUpdateError("sediment-framework 入口冲突，拒绝覆盖")
    aliases = [repo / ".claude/skills", repo / ".grok/skills"]
    for path in aliases:
        if not path.parent.resolve().is_relative_to(repo.resolve()):
            raise SkillsUpdateError("agent 技能目录存在外部路径冲突")
        if ((path.exists() or path.is_symlink()) and
                (not path.is_symlink() or path.resolve() not in
                 (canonical.resolve(), (repo / "frameworks/.claude/skills").resolve()))):
            raise SkillsUpdateError("agent 技能目录冲突，拒绝覆盖")
    canonical.mkdir(parents=True, exist_ok=True)
    versions.mkdir(parents=True, exist_ok=True)
    destination = versions / version
    if not destination.exists():
        (stage / "manifest.json").write_text(json.dumps({"version": version, "skills": names, "source": SOURCE}))
        stage.rename(destination)
    elif _digest(destination, names) != version:
        raise SkillsUpdateError("已发布技能被修改，拒绝覆盖；请保留现场检查")
    for name in names:
        _link(canonical / name, current / ".agents/skills" / name)
    if not own_skill.exists():
        _link(own_skill, sediment)
    _link(current, destination)  # the only switch of the active skill contents
    for name in old_names - set(names):
        (canonical / name).unlink(missing_ok=True)  # only our unchanged stable-target links
    for path in aliases:
        path.parent.mkdir(parents=True, exist_ok=True)
        _link(path, canonical)


def update_finance_skills(repo_root: Path) -> list[str]:
    repo = repo_root.resolve()
    temporary = repo / "_local/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    with (temporary / "finance-skills.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix="finance-skills-stage-", dir=temporary) as work:
            stage = Path(work)
            argv = [*INSTALLER, "add", SOURCE, "--skill", "*", "--yes", "--json",
                    *[arg for agent in AGENTS for arg in ("--agent", agent)]]
            result = subprocess.run(argv, cwd=stage, env=installer_env(repo), check=False,
                                    stdout=subprocess.PIPE, text=True)
            with tempfile.NamedTemporaryFile(mode="w", prefix="finance-skills-installer-", suffix=".json",
                                             dir=temporary, delete=False) as evidence:
                evidence.write(result.stdout)
            if result.returncode:
                raise SkillsUpdateError("finance-skills 安装失败，现有技能入口保持不变")
            names, version = _installation(stage, result.stdout)
            _publish(repo, stage, names, version)
            return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        names = update_finance_skills(args.repo_root)
    except (SkillsUpdateError, OSError) as error:
        print(f"finance-skills 更新失败：{error}")
        return 1
    print(f"finance-skills 已更新：{len(names)} 个技能，四种 agent 共用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
