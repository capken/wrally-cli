import pytest
from conftest import set_web_copy

from wrally.api.models import Shot
from wrally.commands.pull import merge, summary
from wrally.errors import PreflightError
from wrally.workspace import Workspace

IDS = {"project_id": "proj", "series_id": "ser"}


@pytest.fixture
def shots(stories):
    return [Shot.from_story(s) for s in stories]


@pytest.fixture
def ws(tmp_path):
    """空工作区（覆盖 conftest 里已经 pull 过的那个）。"""
    return Workspace(tmp_path)


def snapshot(ws):
    """清单和提示词是状态；.wrally/ 下是每次运行的报告，不算在内。"""
    files = [ws.manifest_path, *sorted((ws.root / "prompts").glob("*"))]
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}


def test_first_pull_builds_manifest_and_prompts(ws, shots):
    report = merge(ws, shots, title="怎样活才安宁", **IDS)
    data = ws.load()
    assert (data["project_id"], data["series_id"], data["title"]) == ("proj", "ser", "怎样活才安宁")
    assert [s["key"] for s in data["shots"]] == ["00", "01", "02", "99"]
    assert data["shots"][2]["image_prompt"] == "prompts/02.image.md"
    assert data["shots"][2]["keyframe"]["asset_id"] == "key02"
    assert "keyframe" not in data["shots"][1]
    assert ws.read_text("prompts/02.image.md") == "画面 02「马赛克」\n"
    assert ws.read_text("prompts/00.anim.md") == "动画 片头\n"
    assert ws.read_text("prompts/99.audio.md") == "旁白 片尾\n"
    assert (report.new_shots, report.prompts_written, report.kept_local) == (4, 12, 0)
    assert summary(report) == "pulled 4 shots: 4 new, 12 prompts written, 0 kept local  -> episode.yaml"


def test_manifest_is_plain_utf8_with_quoted_keys(ws, shots):
    merge(ws, shots, title="怎样活才安宁", **IDS)
    text = ws.manifest_path.read_text(encoding="utf-8")
    assert 'key: "02"' in text and "怎样活才安宁" in text and "\\u" not in text
    assert "http" not in text and "画面" not in text  # 清单只存引用，不存提示词和地址


def test_pull_is_idempotent(ws, shots):
    merge(ws, shots, **IDS)
    before = snapshot(ws)
    report = merge(ws, shots, **IDS)
    assert snapshot(ws) == before
    assert (report.new_shots, report.prompts_written) == (0, 0)
    assert all(r.image == "same" for r in report.rows)


def test_local_prompt_edits_are_kept_unless_forced(ws, shots):
    merge(ws, shots, **IDS)
    ws.write_text("prompts/02.image.md", "本地改过的提示词\n")
    report = merge(ws, shots, **IDS)
    assert ws.read_text("prompts/02.image.md") == "本地改过的提示词\n"
    assert report.kept_local == 1
    assert report.table()[0][0] == "02"  # 有变化的行排在最前
    forced = merge(ws, shots, force=True, **IDS)
    assert ws.read_text("prompts/02.image.md") == "画面 02「马赛克」\n"
    assert forced.prompts_written == 1


def test_local_manifest_fields_and_comments_survive(ws, shots):
    merge(ws, shots, **IDS)
    text = ws.manifest_path.read_text(encoding="utf-8")
    text = text.replace('  - key: "01"', '  # 这一镜要重做\n  - key: "01"\n    narration: 台词')
    text = text.replace("asset_id: key02", "asset_id: mine  # 手选")
    ws.manifest_path.write_text(text, encoding="utf-8")
    merge(ws, shots, **IDS)
    after = ws.manifest_path.read_text(encoding="utf-8")
    assert "# 这一镜要重做" in after and "narration: 台词" in after
    assert "asset_id: mine  # 手选" in after  # 远端的选用不覆盖清单


def test_new_remote_shot_is_appended_and_missing_one_is_reported(ws, shots, stories):
    merge(ws, shots[:3], **IDS)
    report = merge(ws, [shots[0], shots[2], shots[3]], **IDS)
    assert [s["key"] for s in ws.load()["shots"]] == ["00", "01", "02", "99"]
    assert report.new_shots == 1
    notes = {r.shot: r.note for r in report.rows}
    assert notes["99"] == "new shot" and notes["01"] == "missing on remote"


def test_series_mismatch_is_a_preflight_error(ws, shots):
    merge(ws, shots, **IDS)
    with pytest.raises(PreflightError) as e:
        merge(ws, shots, project_id="proj", series_id="other")
    assert e.value.exit_code == 2


def test_duplicate_keys_are_rejected(ws, shots):
    clash = shots[1].model_copy(update={"story_id": "dup"})
    with pytest.raises(PreflightError) as e:
        merge(ws, [*shots, clash], **IDS)
    assert e.value.code == "duplicate_shot_key"
    assert ws.load() is None  # 没有写下半截状态


def test_no_defaults_hint(ws, shots):
    assert any("defaults" in n for n in merge(ws, shots, **IDS).notes)


def test_web_copy_that_differs_from_the_plain_prompt_is_flagged(ws, stories):
    set_web_copy(stories[1], "网页上的旧文本")
    set_web_copy(stories[2], stories[2]["image_prompt"])  # 两份一致的不用提示
    report = merge(ws, [Shot.from_story(s) for s in stories], **IDS)
    notes = {r.shot: r.note for r in report.rows}
    assert "web copy differs" in notes["01"] and "web copy differs" not in notes["02"]
    assert any("web copy differs" in n and "push cannot update" in n for n in report.notes)


def test_web_copy_of_the_anim_prompt_is_flagged_separately(ws, stories):
    set_web_copy(stories[1], "网页上的旧动画", "animation_prompt_json")
    set_web_copy(stories[2], stories[2]["animation_prompt"], "animation_prompt_json")  # 一致的不用提示
    report = merge(ws, [Shot.from_story(s) for s in stories], **IDS)
    notes = {r.shot: r.note for r in report.rows}
    assert notes["01"].endswith("web anim copy differs") and "web copy differs" not in notes["01"]
    assert "copy differs" not in notes["02"]
    assert any("web anim copy differs" in n and "video generation" in n for n in report.notes)
