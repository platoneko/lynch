from __future__ import annotations

from pathlib import Path

from lynch_reader.chat.approvals import (
    APPROVABLE_SKILL, AUTO_DENY_MESSAGE, USER_DENY_MESSAGE, approvable_path, edit_diff, new_approval,
)
from lynch_reader.chat.items import DENIAL_MARKERS


def skill(root: Path) -> Path:
    d = root / APPROVABLE_SKILL / "references"
    d.mkdir(parents=True)
    (d / "r.md").write_text("one\nalpha\nthree\n")
    return d / "r.md"


def test_edit_and_write_under_the_skill_are_approvable(tmp_path):
    f = skill(tmp_path)
    assert approvable_path("Edit", {"file_path": str(f)}, tmp_path) == f.resolve()
    new = tmp_path / "frameworks/.claude/skills/sediment-framework/references/new.md"
    assert approvable_path("Write", {"file_path": str(new)}, tmp_path) == new.resolve()


def test_a_relative_path_is_not_approvable(tmp_path):
    # Claude Code keeps its working directory between Bash calls (`cd frameworks` is allowed), so the server
    # cannot tell what a relative path means to it.
    skill(tmp_path)
    rel = "frameworks/.claude/skills/sediment-framework/references/r.md"
    assert approvable_path("Edit", {"file_path": rel}, tmp_path) is None


def test_other_tools_and_paths_are_not(tmp_path):
    f = skill(tmp_path)
    assert approvable_path("Read", {"file_path": str(f)}, tmp_path) is None
    assert approvable_path("Bash", {"command": "ls"}, tmp_path) is None
    assert approvable_path("Edit", {"file_path": str(tmp_path / "eval/lib/gordon.py")}, tmp_path) is None
    assert approvable_path("Edit", {"file_path": str(tmp_path / "frameworks/.claude/skills/other/SKILL.md")},
                           tmp_path) is None
    assert approvable_path("Edit", {}, tmp_path) is None
    assert approvable_path("Edit", {"file_path": str(tmp_path / "frameworks/.claude/skills/sediment-framework-evil/x.md")},
                           tmp_path) is None


def test_the_repo_root_skills_symlink_is_approvable(tmp_path):
    f = skill(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/skills").symlink_to("../frameworks/.claude/skills")
    via = tmp_path / ".claude/skills/sediment-framework/references/r.md"
    assert approvable_path("Edit", {"file_path": str(via)}, tmp_path) == f.resolve()


def test_escape_via_dotdot_is_not_approvable(tmp_path):
    skill(tmp_path)
    p = str(tmp_path / "frameworks/.claude/skills/sediment-framework/../../../../eval/x.py")
    assert approvable_path("Edit", {"file_path": p}, tmp_path) is None


def test_symlink_out_of_the_skill_is_not_approvable(tmp_path):
    f = skill(tmp_path)
    (tmp_path / "outside.md").write_text("x")
    (f.parent / "link.md").symlink_to(tmp_path / "outside.md")
    assert approvable_path("Edit", {"file_path": str(f.parent / "link.md")}, tmp_path) is None


def test_symlink_loop_inside_the_skill_is_not_approvable(tmp_path):
    f = skill(tmp_path)
    (f.parent / "a.md").symlink_to(f.parent / "b.md")
    (f.parent / "b.md").symlink_to(f.parent / "a.md")
    assert approvable_path("Edit", {"file_path": str(f.parent / "a.md")}, tmp_path) is None


def test_frameworks_symlinked_from_outside_the_repo_is_approvable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    f = skill(tmp_path / "real-fw")  # real-fw/frameworks/.claude/skills/...
    (repo / "frameworks").symlink_to(tmp_path / "real-fw/frameworks")
    via = repo / APPROVABLE_SKILL / "references/r.md"
    assert approvable_path("Edit", {"file_path": str(via)}, repo) == f.resolve()
    request = {"tool_name": "Edit", "tool_use_id": "toolu_1",
               "input": {"file_path": str(via), "old_string": "alpha", "new_string": "beta"}}
    a = new_approval("req1", request, f.resolve(), repo)
    assert a.path == "frameworks/.claude/skills/sediment-framework/references/r.md"


def test_edit_diff_replaces_the_first_match_with_context(tmp_path):
    f = skill(tmp_path)
    d = edit_diff("Edit", {"old_string": "alpha", "new_string": "beta"}, f, "x/r.md")
    assert d.splitlines() == ["--- a/x/r.md", "+++ b/x/r.md", "@@ -1,3 +1,3 @@", " one", "-alpha", "+beta", " three"]


def test_edit_diff_replace_all(tmp_path):
    f = skill(tmp_path)
    f.write_text("a\na\n")
    d = edit_diff("Edit", {"old_string": "a", "new_string": "b", "replace_all": True}, f, "r.md")
    assert [l for l in d.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---")] == ["-a", "-a", "+b", "+b"]


def test_write_of_a_new_file_is_all_additions(tmp_path):
    new = tmp_path / "new.md"
    d = edit_diff("Write", {"content": "x\ny"}, new, "new.md")
    assert d.splitlines()[-2:] == ["+x", "+y"]


def test_edit_whose_old_text_is_not_found_verbatim_shows_what_it_replaces(tmp_path):
    # Claude Code checked the edit before asking, so it applies (e.g. matched across quote styles).
    f = skill(tmp_path)
    d = edit_diff("Edit", {"old_string": "nope", "new_string": "yes"}, f, "r.md")
    assert d.splitlines() == ["（现文件里找不到这段原文，可能是引号写法不同；以下是它要替换的内容）", "-nope", "+yes"]


def test_edit_with_empty_old_text_on_a_missing_file_is_a_create(tmp_path):
    new = tmp_path / "new.md"
    d = edit_diff("Edit", {"old_string": "", "new_string": "x\ny"}, new, "new.md")
    assert d.splitlines() == ["--- a/new.md", "+++ b/new.md", "@@ -0,0 +1,2 @@", "+x", "+y"]


def test_unchanged_write_says_so(tmp_path):
    f = skill(tmp_path)
    assert edit_diff("Write", {"content": f.read_text()}, f, "r.md") == "（内容没有变化）"


def test_deny_messages_are_recognised_as_denials():
    for m in (AUTO_DENY_MESSAGE, USER_DENY_MESSAGE):
        assert any(marker in m for marker in DENIAL_MARKERS)


def test_new_approval_shows_the_repo_relative_path_and_hides_the_input(tmp_path):
    f = skill(tmp_path)
    request = {"tool_name": "Edit", "tool_use_id": "toolu_1",
               "input": {"file_path": str(f), "old_string": "alpha", "new_string": "beta"}}
    a = new_approval("req1", request, f.resolve(), tmp_path)
    assert a.path == "frameworks/.claude/skills/sediment-framework/references/r.md"
    assert a.public() == {"request_id": "req1", "tool_use_id": "toolu_1", "tool": "Edit", "path": a.path,
                          "diff": a.diff}
    assert a.tool_input == request["input"]
