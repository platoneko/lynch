from pathlib import Path
import tomllib

import pytest

from lynch_reader.chat.grok import GrokSetupError, write_research_home
from tests.chat_helpers import conv
from tests.test_chat_grok import cfg


@pytest.mark.parametrize("relationship", ["same", "legacy-parent", "source-parent"])
def test_overlapping_legacy_skill_alias_does_not_hide_selected_skills(tmp_path, relationship):
    home = tmp_path / "logs/c1.grok-home"
    home.mkdir(parents=True)
    root = tmp_path / "skill-source"
    (root / "nested").mkdir(parents=True)
    source, legacy = ((root, root) if relationship == "same" else
                      ((root / "nested", root) if relationship == "legacy-parent" else (root, root / "nested")))
    source_home = tmp_path / "configured"
    source_home.mkdir()
    (source_home / "skills").symlink_to(source, target_is_directory=True)
    (home / "skills").symlink_to(legacy, target_is_directory=True)
    write_research_home(cfg(tmp_path), conv(), tmp_path / "real", skill_home=source_home)
    settings = tomllib.loads((home / "config.toml").read_text())
    assert "ignore" not in settings["skills"]
    assert (home / "skills").resolve() == legacy.resolve()


@pytest.mark.parametrize("same_target", [True, False])
def test_missing_selected_skill_directory_preserves_legacy_entry(tmp_path, same_target):
    home = tmp_path / "logs/c1.grok-home"
    home.mkdir(parents=True)
    source_home = tmp_path / "missing-source"
    target = source_home / "skills" if same_target else tmp_path / "other-missing"
    (home / "skills").symlink_to(target, target_is_directory=True)
    write_research_home(cfg(tmp_path), conv(), tmp_path / "real", skill_home=source_home)
    settings = tomllib.loads((home / "config.toml").read_text())
    assert "ignore" not in settings["skills"]
    assert (home / "skills").is_symlink()


def test_research_home_keeps_native_trust_and_updates_skill_paths(tmp_path):
    real, custom = tmp_path / "real", tmp_path / "custom [skills]"
    (custom / "skills").mkdir(parents=True)
    old = tmp_path / "logs/c1.grok-home"
    (old / "sessions").mkdir(parents=True)
    (old / "sessions/history").write_text("existing session")
    (old / "trusted_folders.toml").write_text("# existing trust")
    (old / "skills").write_text("user-owned")
    home = write_research_home(cfg(tmp_path), conv(), real, skill_home=custom)
    assert home == old
    assert (home / "sessions/history").read_text() == "existing session"
    assert (home / "trusted_folders.toml").read_text() == "# existing trust"
    assert (home / "skills").read_text() == "user-owned"
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["skills"]["paths"] == [str(custom / "skills")]
    assert "ignore" not in config["skills"]
    assert config["ui"]["permission_mode"] == "dontAsk"


def test_research_home_configuration_changes_preserve_permissions_and_auth(tmp_path):
    real, custom = tmp_path / "real", tmp_path / "custom"
    (real / "skills").mkdir(parents=True)
    (custom / "skills").mkdir(parents=True)
    (real / "auth.json").write_text("fake-auth")
    home = write_research_home(cfg(tmp_path), conv(), real, skill_home=custom)
    before = tomllib.loads((home / "config.toml").read_text())
    assert (home / "auth.json").resolve() == (real / "auth.json").resolve()
    assert write_research_home(cfg(tmp_path), conv(), real) == home
    after = tomllib.loads((home / "config.toml").read_text())
    assert after["skills"]["paths"] == [str(real / "skills")]
    assert before["permission"] == after["permission"]
    assert before["ui"] == after["ui"]


def test_first_trust_write_persists_across_resume(tmp_path):
    real = tmp_path / "real"
    home = write_research_home(cfg(tmp_path), conv(), real)
    trust = home / "trusted_folders.toml"
    assert not trust.exists()
    trust.write_text("# written by grok")
    write_research_home(cfg(tmp_path), conv(), real)
    assert trust.read_text() == "# written by grok"


def test_concurrent_legacy_skill_entry_change_is_not_overwritten(tmp_path, monkeypatch):
    real = tmp_path / "real"
    old = tmp_path / "logs/c1.grok-home"
    old.mkdir(parents=True)
    entry = old / "skills"
    entry.write_text("before")
    original = Path.write_text
    def concurrent_change(self, *args, **kwargs):
        if self.name == "config.toml":
            original(entry, "other process owns this")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", concurrent_change)
    write_research_home(cfg(tmp_path), conv(), real)
    assert entry.read_text() == "other process owns this"


def test_failed_configuration_write_preserves_trust_and_can_retry(tmp_path, monkeypatch):
    real = tmp_path / "real"
    home = tmp_path / "logs/c1.grok-home"
    home.mkdir(parents=True)
    trust = home / "trusted_folders.toml"
    trust.write_text("existing trust")
    original = Path.write_text
    def fail_once(self, *args, **kwargs):
        if self.name == "config.toml":
            monkeypatch.setattr(Path, "write_text", original)
            raise OSError("synthetic interruption")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail_once)
    with pytest.raises(GrokSetupError):
        write_research_home(cfg(tmp_path), conv(), real)
    assert trust.read_text() == "existing trust"
    assert write_research_home(cfg(tmp_path), conv(), real) == home
