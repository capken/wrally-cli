import json

import pytest
from conftest import entry, make_story

from wrally.api.models import Shot
from wrally.commands.pull import merge
from wrally.commands.push import push
from wrally.errors import PreflightError
from wrally.prompts import Styles, compose, split
from wrally.workspace import Workspace

SUFFIX = "马赛克风格，{border}，画面中不出现任何文字"
PREFIX = "镜头固定，{border}边框不动。只有轻微的动态："
NARRATION = "使用<{voice_name}>的音色，纪录片旁白。他说：「{narration}」"
BORDERS = {"wave": {"image": "四周有一圈波浪纹边框", "anim": "波浪纹"},
           "rope": {"image": "四周是一圈绳结编织纹边框", "anim": "绳结编织纹"}}  # fmt: skip
STYLES = Styles(
    image=SUFFIX, anim=PREFIX, audio=NARRATION, borders=BORDERS, default_border="wave", voice_name="旁白男声"
)
WAVE_TAIL = "马赛克风格，四周有一圈波浪纹边框，画面中不出现任何文字"
ROPE_TAIL = "马赛克风格，四周是一圈绳结编织纹边框，画面中不出现任何文字"


def test_keyframe_gets_style_suffix_with_border():
    assert compose(STYLES, {"key": "01"}, "image", "海上的船\n") == "海上的船。" + WAVE_TAIL
    assert compose(STYLES, {"key": "02", "border": "rope"}, "image", "陶缸。") == "陶缸。" + ROPE_TAIL


def test_animation_gets_prefix_with_border():
    assert compose(STYLES, {"key": "02", "border": "rope"}, "anim", "老人转头") == (
        "镜头固定，绳结编织纹边框不动。只有轻微的动态：老人转头"
    )


def test_narration_uses_template_and_swaps_quotes():
    """台词里的「」换成『』，否则会和模板自己的「」冲突。"""
    text = compose(STYLES, {"key": "07"}, "audio", "他说「理性」是向导。")
    assert text == "使用<旁白男声>的音色，纪录片旁白。他说：「他说『理性』是向导。」"


def test_raw_shots_are_sent_as_is():
    shot = {"key": "00", "raw": ["image", "anim"]}
    assert compose(STYLES, shot, "image", "写实长廊\n") == "写实长廊"
    assert compose(STYLES, shot, "anim", "镜头推近") == "镜头推近"


def test_full_text_in_file_is_refused_instead_of_doubled():
    with pytest.raises(PreflightError) as e:
        compose(STYLES, {"key": "01"}, "image", "海上的船。" + WAVE_TAIL)
    assert e.value.code == "already_styled" and e.value.exit_code == 2


def test_unknown_border_is_refused():
    with pytest.raises(PreflightError) as e:
        compose(STYLES, {"key": "01", "border": "gold"}, "image", "x")
    assert e.value.code == "unknown_border"


@pytest.mark.parametrize(("kind", "border", "body"), [
    ("image", "wave", "海上的船"), ("image", "rope", "陶缸旁的老人！"), ("anim", "rope", "老人转头"),
    ("audio", None, "他提着灯，找一个真实的人。"),
])  # fmt: skip
def test_split_is_the_exact_inverse_of_compose(kind, border, body):
    full = compose(STYLES, {"key": "x", "border": border}, kind, body)
    assert split(STYLES, {"key": "x"}, kind, full) == (body, border)


def test_split_refuses_text_that_drifted_from_the_template():
    assert split(STYLES, {"key": "x"}, "image", "海上的船。马赛克风格，画面中不出现任何文字") is None
    assert split(STYLES, {"key": "x"}, "anim", "镜头缓慢推近：老人转头") is None


def styled_workspace(tmp_path, stories) -> Workspace:
    ws = Workspace(tmp_path)
    for name, text in (("s.md", SUFFIX), ("a.md", PREFIX), ("n.md", NARRATION)):
        ws.write_text(f"styles/{name}", text + "\n")
    defaults = {
        "image": {"model": "img9", "resolution": 4, "aspect": "16:9", "count": 2},
        "audio": {"model": "aud1", "voice_name": "旁白男声"},
        "style_suffix": "styles/s.md",
        "anim_prefix": "styles/a.md",
        "narration_template": "styles/n.md",
        "border": "wave",
        "borders": BORDERS,
    }
    ws.manifest_path.write_text(json.dumps({"project_id": "proj", "series_id": "ser", "defaults": defaults},
                                           ensure_ascii=False), encoding="utf-8")  # fmt: skip
    return ws


@pytest.fixture
def styled(tmp_path):
    """远端三镜：01 完全符合公共文本（rope），02 的动画偏离了前缀，99 是写实片尾。"""
    stories = [make_story("01", "s01", 1), make_story("02", "s02", 2), make_story("片尾", "s99", 3)]
    stories[0] |= {"image_prompt": "海上的船。" + ROPE_TAIL,
                   "animation_prompt": "镜头固定，绳结编织纹边框不动。只有轻微的动态：海面起伏"}  # fmt: skip
    stories[1] |= {"image_prompt": "陶缸。" + WAVE_TAIL, "animation_prompt": "镜头缓慢推近，老人转头"}
    stories[2] |= {"image_prompt": "写实长廊，冷灰色调", "animation_prompt": "镜头推近到铭牌"}
    for story in stories:
        extra = json.loads(story["extra"])
        extra["audio_prompt"] = "使用<旁白男声>的音色，纪录片旁白。他说：「台词" + story["story_num"] + "」"
        story["extra"] = json.dumps(extra, ensure_ascii=False)
    return styled_workspace(tmp_path, stories), stories


def pull(ws, stories, **kw):
    return merge(ws, [Shot.from_story(s) for s in stories], project_id="proj", series_id="ser", **kw)


def test_pull_stores_only_the_unique_part_and_detects_border(styled):
    ws, stories = styled
    pull(ws, stories)
    first = entry(ws, "01")
    assert ws.read_text("prompts/01.image.md") == "海上的船\n"
    assert ws.read_text("prompts/01.anim.md") == "海面起伏\n"
    assert first["border"] == "rope" and "raw" not in first
    assert first["narration"] == "台词01" and "audio_prompt" not in first
    assert not (ws.root / "prompts/01.audio.md").exists()


def test_pull_marks_drifted_prompts_raw_and_keeps_full_text(styled):
    ws, stories = styled
    pull(ws, stories)
    assert list(entry(ws, "02")["raw"]) == ["anim"]
    assert ws.read_text("prompts/02.anim.md") == "镜头缓慢推近，老人转头\n"
    assert ws.read_text("prompts/02.image.md") == "陶缸\n" and "border" not in entry(ws, "02")
    assert list(entry(ws, "99")["raw"]) == ["image", "anim"]
    assert "raw: [image, anim]" in ws.manifest_path.read_text(encoding="utf-8")


def test_styled_pull_is_idempotent_and_sees_local_edits(styled):
    ws, stories = styled
    pull(ws, stories)
    before = ws.manifest_path.read_bytes()
    again = pull(ws, stories)
    assert ws.manifest_path.read_bytes() == before
    assert all((r.image, r.anim, r.audio) == ("same", "same", "same") for r in again.rows)
    ws.write_text("styles/s.md", "新风格，{border}\n")  # 改一处公共文本，所有拼接的镜头都待 push
    states = {r.shot: r.image for r in pull(ws, stories).rows}
    assert states == {"01": "local", "02": "local", "99": "same"}


async def test_push_sends_only_differences_in_the_fixed_order(styled, remote):
    """edit_content 必须在 set_model 之前，否则生成时报「未配置模型」。"""
    ws, stories = styled
    remote.stories = stories
    pull(ws, stories)
    ws.write_text("prompts/01.image.md", "夜里的船\n")
    report = await push(ws, remote, "prompts")
    assert report.summary == "pushed prompts: 1 shots (image 1, anim 0), 0 failed"
    assert report.rows[0] == ("01", "push", "same")
    assert remote.actions() == ["edit_content", "set_model"]
    [edit] = next(a["edits"] for t, a in remote.calls if a.get("action") == "edit_content")
    assert edit == {"story_id": "s01", "image_prompt": "夜里的船。" + ROPE_TAIL}
    config = next(a["model_config"] for t, a in remote.calls if a.get("action") == "set_model")
    assert config == {
        "story_ids": ["s01"],
        "image": {"model": "img9", "count": 2, "aspectRatio": "16:9", "resolution": 4},
    }
    assert Shot.from_story(stories[0]).gen_config["image"]["model"] == "img9"

    # FakeWorkRally 不落盘原始返回，这里只确认没有写入时不会留下空的备份记录
    assert report.backup == ""

    remote.calls.clear()
    again = await push(ws, remote, "prompts")
    assert again.shots == 0 and remote.actions() == []  # 重跑不再写


async def test_push_dry_run_writes_nothing(styled, remote):
    ws, stories = styled
    remote.stories = stories
    pull(ws, stories)
    ws.write_text("prompts/02.anim.md", "镜头缓慢推近，老人回头\n")
    report = await push(ws, remote, "prompts", dry_run=True)
    assert report.summary == "dry run: would push prompts: 1 shots (image 0, anim 1), 0 failed"
    assert remote.actions() == [] and stories[1]["animation_prompt"] == "镜头缓慢推近，老人转头"


async def test_push_audio_prompt_rewrites_audio_model_with_capabilities(styled, remote):
    ws, stories = styled
    remote.stories = stories
    pull(ws, stories)
    data = ws.load()
    next(e for e in data["shots"] if e["key"] == "02")["narration"] = "他说「够了」。"
    ws.save(data)
    report = await push(ws, remote, "audio-prompt", "02")
    assert report.counts == {"audio": 1} and remote.actions() == ["edit_content", "set_model"]
    assert json.loads(stories[1]["extra"])["audio_prompt"].endswith("他说：「他说『够了』。」")
    config = [a["model_config"] for t, a in remote.calls if a.get("action") == "set_model"][0]
    assert config == {"story_ids": ["s02"], "audio": {"model": "aud1", "count": 1, "modelCapabilities": [14]}}


async def test_push_refuses_before_writing_when_a_prompt_cannot_be_composed(styled, remote):
    ws, stories = styled
    remote.stories = stories
    pull(ws, stories)
    ws.write_text("prompts/01.image.md", "海上的船。" + ROPE_TAIL + "\n")  # 有人把完整文本贴了回来
    ws.write_text("prompts/02.image.md", "新的陶缸\n")
    with pytest.raises(PreflightError) as e:
        await push(ws, remote, "prompts")
    assert e.value.code == "already_styled" and remote.calls == []


async def test_push_without_image_defaults_is_refused_before_writing(styled, remote):
    ws, stories = styled
    remote.stories = stories
    pull(ws, stories)
    data = ws.load()
    del data["defaults"]["image"]
    ws.save(data)
    ws.write_text("prompts/01.image.md", "夜里的船\n")
    with pytest.raises(PreflightError) as e:
        await push(ws, remote, "prompts")
    assert e.value.code == "missing_defaults" and remote.actions() == []


async def test_push_keeps_a_copy_of_remote_before_overwriting(styled, remote):
    """覆盖远端提示词前的原文要另存，且不被下一次运行冲掉。"""
    ws, stories = styled
    remote.stories = stories
    pull(ws, stories)
    (ws.raw_dir).mkdir(parents=True, exist_ok=True)
    (ws.raw_dir / "push-list-p1.json").write_text("第一次写入前的远端原文", encoding="utf-8")
    ws.write_text("prompts/01.image.md", "夜里的船\n")
    first = await push(ws, remote, "prompts")
    (ws.raw_dir / "push-list-p1.json").write_text("后来的状态", encoding="utf-8")
    await push(ws, remote, "prompts")  # 没有差异，不产生新备份
    backups = sorted((ws.raw_dir / "history").glob("push-*-before-p1.json"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "第一次写入前的远端原文"
    assert first.backup.startswith(".wrally/raw/history/push-") and any("backup" in n for n in first.notes)
