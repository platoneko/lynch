from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def updater():
    path = Path(__file__).resolve().parents[3] / 'scripts/update_finance_skills.py'
    spec = importlib.util.spec_from_file_location('update_finance_skills', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project(root):
    source = root / 'frameworks/.claude/skills'
    for name in ('sediment-framework', 'earnings-recap', 'new-finance'):
        (source / name).mkdir(parents=True)
        (source / name / 'SKILL.md').write_text(name)
    own = root / '.agents/skills/discover-kol'
    own.mkdir(parents=True)
    (own / 'SKILL.md').write_text('discovery')
    (root / '.claude').mkdir()
    (root / '.claude/skills').symlink_to('../.agents/skills')
    return source


def content_updater(module, monkeypatch):
    real = module._updater(Path(__file__).resolve().parents[3])
    calls = []

    def update(directory, *, on_updated):
        calls.append(directory)
        names = ['earnings-recap', 'new-finance']
        on_updated(names)
        return names

    monkeypatch.setattr(module, '_updater', lambda _: SimpleNamespace(
        update_finance_skills=update, _link=real._link, SkillsUpdateError=real.SkillsUpdateError))
    return calls


def test_app_delegates_to_content_repo_and_reads_its_files(tmp_path, monkeypatch):
    module = updater()
    source = project(tmp_path)
    calls = content_updater(module, monkeypatch)
    assert module.update_finance_skills(tmp_path) == ['earnings-recap', 'new-finance']
    assert calls == [tmp_path / 'frameworks']
    for prefix in ('.agents', '.claude', '.grok'):
        for name in ('earnings-recap', 'new-finance', 'sediment-framework'):
            assert (tmp_path / prefix / 'skills' / name).resolve() == source / name
    assert (tmp_path / '.agents/skills/discover-kol/SKILL.md').read_text() == 'discovery'
    assert not (tmp_path / 'output/finance-skills').exists()


def test_old_runtime_links_migrate_without_removing_old_runtime_files(tmp_path, monkeypatch):
    module = updater()
    source = project(tmp_path)
    old = tmp_path / 'output/finance-skills/current/.agents/skills'
    for name in ('earnings-recap', 'obsolete'):
        (old / name).mkdir(parents=True)
        (old / name / 'SKILL.md').write_text('old')
        (tmp_path / '.agents/skills' / name).symlink_to(old / name)
    content_updater(module, monkeypatch)
    module.update_finance_skills(tmp_path)
    assert (tmp_path / '.agents/skills/earnings-recap').resolve() == source / 'earnings-recap'
    assert not (tmp_path / '.agents/skills/obsolete').is_symlink()
    assert (old / 'obsolete/SKILL.md').read_text() == 'old'


@pytest.mark.parametrize('kind', ['directory', 'foreign-link', 'agent-directory'])
def test_unknown_entries_are_preserved(tmp_path, monkeypatch, kind):
    module = updater()
    source = project(tmp_path)
    entry = tmp_path / '.agents/skills/earnings-recap'
    if kind == 'foreign-link':
        entry.symlink_to(source / 'new-finance')
    else:
        if kind == 'agent-directory':
            entry = tmp_path / '.claude/skills'
            entry.unlink()
        entry.mkdir()
        (entry / 'private').write_text('keep')
    content_updater(module, monkeypatch)
    with pytest.raises(module.SkillsUpdateError, match='冲突'):
        module.update_finance_skills(tmp_path)
    assert entry.exists()
    if not entry.is_symlink():
        assert (entry / 'private').read_text() == 'keep'


def test_missing_content_updater_fails_clearly(tmp_path):
    module = updater()
    with pytest.raises(module.SkillsUpdateError, match='lynch-frameworks'):
        module.update_finance_skills(tmp_path)


def test_discovery_skill_link_is_preserved(tmp_path, monkeypatch):
    module = updater()
    project(tmp_path)
    discovery = tmp_path / '.agents/skills/discover-kol'
    source = tmp_path / 'frameworks/.claude/skills/discover-kol'
    discovery.rename(source)
    discovery.symlink_to('../../frameworks/.claude/skills/discover-kol')
    content_updater(module, monkeypatch)
    module.update_finance_skills(tmp_path)
    assert discovery.is_symlink()
    assert (discovery / 'SKILL.md').read_text() == 'discovery'


def test_partial_binding_failure_restores_all_prior_entries(tmp_path, monkeypatch):
    module = updater()
    project(tmp_path)
    real = module._updater(Path(__file__).resolve().parents[3])
    before = {p: p.readlink() for p in (tmp_path / '.agents/skills').iterdir() if p.is_symlink()}
    link = real._link

    def interrupt(path, target):
        if path == tmp_path / '.agents/skills/new-finance':
            raise OSError('binding failed')
        return link(path, target)

    calls = content_updater(module, monkeypatch)
    fake = module._updater(tmp_path)
    fake._link = interrupt
    monkeypatch.setattr(module, '_updater', lambda _: fake)
    with pytest.raises(OSError, match='binding failed'):
        module.update_finance_skills(tmp_path)
    after = {p: p.readlink() for p in (tmp_path / '.agents/skills').iterdir() if p.is_symlink()}
    assert before == after
