"""项目文件 wrally.yaml：各集共用的 defaults、rules 和 wrally 不认识的小节，单集清单覆盖。"""

import json

import anyio
import pytest
from conftest import FakeWorkRally, entry, make_story
from test_prompts_push import BORDERS, NARRATION, PREFIX, SUFFIX, pull

from wrally.commands.export import collect
from wrally.commands.lint import lint
from wrally.commands.new import new
from wrally.commands.push import image_config
from wrally.errors import PreflightError
from wrally.prompts import composed, load_styles
from wrally.rules import load_rules
from wrally.workspace import Workspace, overlay

PROJECT = {
    "project_id": "proj",
    "defaults": {
        "image": {"model": "img9", "resolution": 4, "aspect": "16:9", "count": 2},
        "audio": {"model": "aud1", "voice_name": "旁白男声"},
        "style_suffix": "styles/s.md",
        "anim_prefix": "styles/a.md",
        "narration_template": "styles/n.md",
        "border": "wave",
        "borders": BORDERS,
    },
    "rules": {"f0_max": 220},
    "assemble": {"bgm": "assets/bgm.wav", "atempo": 1.0},
}


def write_project(show, content=PROJECT):
    show.mkdir(parents=True, exist_ok=True)
    (show / "wrally.yaml").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def show(tmp_path):
    """一部剧的目录：项目文件、公共文本和配乐在上面，单集工作区在 ep5/。"""
    root = tmp_path / "show"
    write_project(root)
    for name, text in (("s.md", SUFFIX), ("a.md", PREFIX), ("n.md", NARRATION)):
        (root / "styles").mkdir(exist_ok=True)
        (root / "styles" / name).write_text(text + "\n", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "bgm.wav").write_bytes(b"x")
    return root


@pytest.fixture
def ep(show):
    ws = Workspace(show / "ep5")
    ws.root.mkdir()
    return ws


def edit(ws, **fields):
    data = ws.load()
    data.update(fields)
    ws.save(data)


def test_overlay_merges_mappings_and_replaces_everything_else():
    base = {"image": {"model": "a", "count": 2}, "titles": [1, 2], "border": "wave"}
    merged = overlay(base, {"image": {"count": 4}, "titles": [3], "border": None})
    assert merged == {"image": {"model": "a", "count": 4}, "titles": [3], "border": None}
    assert base["image"] == {"model": "a", "count": 2}  # 不改动原来的


def test_workspace_without_a_project_file_behaves_as_before(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.project() == {} and ws.settings({"defaults": {"border": "x"}}, "defaults") == {"border": "x"}


def test_pull_splits_prompts_with_styles_from_the_project_file(ep):
    stories = [make_story("01", "s01", 1)]
    stories[0] |= {
        "image_prompt": "海上的船。" + SUFFIX.replace("{border}", BORDERS["rope"]["image"]),
        "animation_prompt": PREFIX.replace("{border}", BORDERS["rope"]["anim"]) + "海面起伏",
    }
    report = pull(ep, stories)
    assert ep.read_text("prompts/01.image.md") == "海上的船\n" and entry(ep, "01")["border"] == "rope"
    assert not any("defaults" in n for n in report.notes)
    # 项目文件里的内容不会被抄进单集清单
    assert "defaults" not in ep.load() and "rules" not in ep.load()
    before = ep.manifest_path.read_bytes()
    pull(ep, stories)
    assert ep.manifest_path.read_bytes() == before


def test_episode_overrides_one_key_and_keeps_the_rest(ep):
    pull(ep, [make_story("01", "s01", 1)])
    edit(ep, defaults={"image": {"count": 4}, "border": "rope"})
    defaults = ep.settings(ep.load(), "defaults")
    assert image_config(defaults)["count"] == 4 and defaults["image"]["model"] == "img9"
    assert load_styles(ep, ep.load()).default_border == "rope"


def test_episode_can_switch_off_a_shared_style_with_null(ep):
    pull(ep, [make_story("01", "s01", 1)])
    edit(ep, defaults={"style_suffix": None})
    ep.write_text("prompts/01.image.md", "手写的完整提示词\n")
    data = ep.load()
    target = next(e for e in data["shots"] if e["key"] == "01")
    target.pop("raw", None)
    assert composed(ep, load_styles(ep, data), target, "image") == "手写的完整提示词"


def test_sections_wrally_does_not_know_are_merged_and_exported_as_written(ep):
    """wrally 不知道这些小节里哪些值是路径：原样带出，另外告诉读的一方项目文件在哪个目录。"""
    pull(ep, [make_story("01", "s01", 1)])
    content = collect(ep, ep.load())
    assert content["extra"] == {"assemble": {"bgm": "assets/bgm.wav", "atempo": 1.0}}
    assert content["project_dir"] == ".."
    # 单集清单逐键覆盖，写 null 表示这一集不要这一项
    edit(ep, assemble={"bgm": "own.wav", "atempo": None}, credits=["甲", "乙"])
    content = collect(ep, ep.load())
    assert content["extra"] == {"assemble": {"bgm": "own.wav", "atempo": None}, "credits": ["甲", "乙"]}
    assert "defaults" not in content["extra"] and "project_id" not in content["extra"]


def test_missing_style_file_in_the_project_is_a_preflight_error(ep, show):
    (show / "styles" / "s.md").unlink()
    with pytest.raises(PreflightError) as e:
        load_styles(ep, {})
    assert e.value.code == "style_file_missing"


def test_rules_come_from_the_project_and_the_episode_wins(ep):
    pull(ep, [make_story("01", "s01", 1)])
    assert load_rules(ep, ep.load()).f0_max == 220
    edit(ep, rules={"f0_max": 180})
    rules = load_rules(ep, ep.load())
    assert rules.f0_max == 180 and rules.slow_max is None  # 没写的一项不查
    assert lint(ep).errors == 0


def test_nearest_project_file_wins_and_it_must_be_a_mapping(ep, show):
    write_project(show.parent, {"project_id": "outer"})
    assert ep.project()["project_id"] == "proj"
    (show / "wrally.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(PreflightError) as e:
        Workspace(ep.root).project()
    assert e.value.code == "bad_project_file"


def test_new_takes_the_project_id_from_the_project_file(ep):
    remote = FakeWorkRally([])
    anyio.run(lambda: new(ep, remote, "01-02", name="EP5"))
    assert ep.load()["project_id"] == "proj" and [s["key"] for s in ep.load()["shots"]] == ["01", "02"]
    assert not (ep.root.parent / "wrally.yaml").read_text(encoding="utf-8").count("series_id")
