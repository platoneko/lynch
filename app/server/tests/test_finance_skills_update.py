from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def updater():
    path = Path(__file__).resolve().parents[3] / "scripts/update_finance_skills.py"
    spec = importlib.util.spec_from_file_location("update_finance_skills", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project(tmp_path):
    source = tmp_path / "frameworks/.claude/skills"
    for name in ("sediment-framework", "funda-data"):
        (source / name).mkdir(parents=True)
        (source / name / "SKILL.md").write_text(name)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/skills").symlink_to("../frameworks/.claude/skills")
    local = tmp_path / ".agents/skills/discover-kol"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("local-discovery")
    return source


def installer(names=("earnings-recap", "new-finance-skill"), *, fail=False):
    def run(argv, *, cwd, env, check, stdout, text):
        assert "skills@1.7.6" in argv and "himself65/finance-skills" in argv
        assert "--json" in argv and "--yes" in argv and "*" in argv
        assert all(name in argv for name in ("claude-code", "codex", "grok", "opencode"))
        assert (Path(cwd) / ".agents/skills").resolve() != (Path(cwd).parents[2] / ".agents/skills").resolve()
        assert "PROVIDER_PRIVATE_TOKEN" not in env
        if fail:
            return SimpleNamespace(returncode=1, stdout="[]")
        for name in names:
            skill = Path(cwd) / ".agents/skills" / name
            (skill / "references").mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n")
            (skill / "references/data.md").write_text("reference")
        lock = {name: {"source": "himself65/finance-skills", "computedHash": "fake-hash"} for name in names}
        (Path(cwd) / "skills-lock.json").write_text(json.dumps({"version": 1, "skills": lock}))
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {"name": name, "status": "installed", "source": "himself65/finance-skills",
             "agents": ["Claude Code", "Codex", "Grok Build", "OpenCode"]} for name in names]))
    return run


def test_install_publishes_shared_skills_without_changing_frameworks(tmp_path, monkeypatch):
    module = updater()
    original = project(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", installer())
    monkeypatch.setenv("PROVIDER_PRIVATE_TOKEN", "fake-value")
    names = module.update_finance_skills(tmp_path)
    assert names == ["earnings-recap", "new-finance-skill"]
    assert (original / "funda-data/SKILL.md").read_text() == "funda-data"
    assert (original / "sediment-framework/SKILL.md").read_text() == "sediment-framework"
    assert (tmp_path / ".agents/skills/discover-kol/SKILL.md").read_text() == "local-discovery"
    for prefix in (".agents", ".claude", ".grok"):
        for name in names:
            skill = tmp_path / prefix / "skills" / name
            assert (skill / "SKILL.md").is_file()
            assert (skill / "references/data.md").read_text() == "reference"
            assert (skill / "SKILL.md").resolve().is_relative_to(tmp_path / "output/finance-skills/versions")
        assert (tmp_path / prefix / "skills/sediment-framework/SKILL.md").read_text() == "sediment-framework"


def test_refresh_removes_only_owned_stale_links_and_preserves_old_version(tmp_path, monkeypatch):
    module = updater()
    project(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", installer(("old-skill",)))
    module.update_finance_skills(tmp_path)
    old = (tmp_path / ".agents/skills/old-skill").resolve()
    monkeypatch.setattr(module.subprocess, "run", installer(("new-skill",)))
    module.update_finance_skills(tmp_path)
    assert not (tmp_path / ".agents/skills/old-skill").is_symlink()
    assert (old / "SKILL.md").is_file()
    assert (tmp_path / ".agents/skills/new-skill/SKILL.md").is_file()


def test_install_failure_keeps_existing_entries(tmp_path, monkeypatch):
    module = updater()
    project(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", installer(fail=True))
    with pytest.raises(module.SkillsUpdateError):
        module.update_finance_skills(tmp_path)
    assert (tmp_path / ".claude/skills").readlink() == Path("../frameworks/.claude/skills")
    assert not (tmp_path / ".grok/skills").exists()


@pytest.mark.parametrize("conflict", ["skill-directory", "claude-directory", "foreign-link"])
def test_conflicting_user_paths_are_preserved(tmp_path, monkeypatch, conflict):
    module = updater()
    project(tmp_path)
    entry = tmp_path / ".agents/skills/earnings-recap"
    if conflict == "skill-directory":
        entry.mkdir()
        (entry / "mine.txt").write_text("keep")
    elif conflict == "foreign-link":
        entry.symlink_to(tmp_path / "frameworks/.claude/skills/funda-data")
    else:
        entry = tmp_path / ".claude/skills"
        entry.unlink()
        entry.mkdir()
        (entry / "mine.txt").write_text("keep")
    monkeypatch.setattr(module.subprocess, "run", installer())
    with pytest.raises(module.SkillsUpdateError, match="冲突|覆盖"):
        module.update_finance_skills(tmp_path)
    assert entry.exists()
    if not entry.is_symlink():
        assert (entry / "mine.txt").read_text() == "keep"


def test_empty_successful_install_is_rejected(tmp_path, monkeypatch):
    module = updater()
    project(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", installer(()))
    with pytest.raises(module.SkillsUpdateError, match="技能"):
        module.update_finance_skills(tmp_path)
    assert (tmp_path / ".claude/skills").readlink() == Path("../frameworks/.claude/skills")


def test_interrupted_publication_can_retry_without_losing_active_version(tmp_path, monkeypatch):
    module = updater()
    project(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", installer(("old-skill",)))
    module.update_finance_skills(tmp_path)
    original = module._link

    def interrupt(path, target):
        if path.name == "current":
            raise OSError("simulated publication interruption")
        original(path, target)

    monkeypatch.setattr(module.subprocess, "run", installer(("new-skill",)))
    monkeypatch.setattr(module, "_link", interrupt)
    with pytest.raises(OSError):
        module.update_finance_skills(tmp_path)
    assert (tmp_path / ".agents/skills/old-skill/SKILL.md").is_file()
    monkeypatch.setattr(module, "_link", original)
    assert module.update_finance_skills(tmp_path) == ["new-skill"]


@pytest.mark.parametrize("kind", ["file", "directory", "foreign-link"])
def test_unknown_current_entry_is_preserved(tmp_path, monkeypatch, kind):
    module = updater()
    project(tmp_path)
    current = tmp_path / "output/finance-skills/current"
    current.parent.mkdir(parents=True)
    if kind == "file":
        current.write_text("private-current")
    elif kind == "directory":
        current.mkdir()
        (current / "mine").write_text("keep")
    else:
        current.symlink_to(tmp_path / "frameworks")
    monkeypatch.setattr(module.subprocess, "run", installer())
    with pytest.raises(module.SkillsUpdateError, match="冲突|覆盖"):
        module.update_finance_skills(tmp_path)
    assert current.exists()
    if kind == "file":
        assert current.read_text() == "private-current"
    elif kind == "foreign-link":
        assert current.readlink() == tmp_path / "frameworks"
    else:
        assert (current / "mine").read_text() == "keep"


def test_external_versions_directory_is_not_written(tmp_path, monkeypatch):
    module = updater()
    project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    versions = tmp_path / "output/finance-skills/versions"
    versions.parent.mkdir(parents=True)
    versions.symlink_to(outside)
    monkeypatch.setattr(module.subprocess, "run", installer())
    with pytest.raises(module.SkillsUpdateError, match="冲突|覆盖"):
        module.update_finance_skills(tmp_path)
    assert list(outside.iterdir()) == []
