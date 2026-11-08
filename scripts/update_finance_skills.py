#!/usr/bin/env python3
"""Delegate finance-skill updates to lynch-frameworks and expose its files to the app."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path


class SkillsUpdateError(Exception):
    pass


def _updater(repo: Path):
    path = repo / 'frameworks/scripts/update_finance_skills.py'
    if not path.is_file():
        raise SkillsUpdateError('内容仓库缺少技能更新器，请先更新 lynch-frameworks')
    spec = importlib.util.spec_from_file_location('framework_finance_skills', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind(repo: Path, names: list[str], updater) -> None:
    canonical = repo / '.agents/skills'
    source = repo / 'frameworks/.claude/skills'
    if not canonical.resolve().is_relative_to(repo):
        raise SkillsUpdateError('共享技能目录存在外部路径冲突')

    def managed(path: Path) -> bool:
        if path.name == 'discover-kol' or not path.is_symlink():
            return False
        target = Path(os.path.abspath(path.parent / path.readlink()))
        return target in (source / path.name, repo / 'output/finance-skills/current/.agents/skills' / path.name)

    old = {p.name for p in canonical.iterdir() if managed(p)} if canonical.is_dir() else set()
    targets = {n: source / n for n in [*names, 'sediment-framework']}
    for name in set(targets) | old:
        path = canonical / name
        if ((path.exists() or path.is_symlink()) and not managed(path)
                and not (name == 'sediment-framework' and path.is_symlink()
                         and path.resolve() == targets[name].resolve())):
            raise SkillsUpdateError(f'技能入口冲突，拒绝覆盖：{name}')
    aliases = [repo / '.claude/skills', repo / '.grok/skills']
    for path in aliases:
        if (not path.parent.resolve().is_relative_to(repo)
                or ((path.exists() or path.is_symlink()) and
                    (not path.is_symlink() or path.resolve() not in (canonical.resolve(), source.resolve())))):
            raise SkillsUpdateError('agent 技能目录冲突，拒绝覆盖')
    canonical.mkdir(parents=True, exist_ok=True)
    paths = [*(canonical / name for name in set(targets) | old), *aliases]
    previous = {path: path.readlink() if path.is_symlink() else None for path in paths}
    try:
        for name, target in targets.items():
            updater._link(canonical / name, target)
        for name in old - set(targets):
            (canonical / name).unlink()
        for path in aliases:
            updater._link(path, canonical)
    except BaseException:
        for path, target in previous.items():
            if target is not None:
                updater._link(path, path.parent / target)
            elif path.is_symlink():
                path.unlink()
        raise


def update_finance_skills(repo_root: Path) -> list[str]:
    repo = repo_root.resolve()
    updater = _updater(repo)
    try:
        names = updater.update_finance_skills(repo / 'frameworks', on_updated=lambda names: _bind(repo, names, updater))
    except updater.SkillsUpdateError as error:
        raise SkillsUpdateError(str(error)) from error
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    args = parser.parse_args()
    try:
        names = update_finance_skills(args.repo_root)
    except (SkillsUpdateError, OSError) as error:
        print(f'finance-skills 更新失败：{error}')
        return 1
    print(f'finance-skills 已更新：{len(names)} 个技能，直接共用 lynch-frameworks 的文件')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
