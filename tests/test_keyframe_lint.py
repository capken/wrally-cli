import io

import pytest
from conftest import entry
from PIL import Image

from wrally.commands import keyframe
from wrally.commands.lint import find_words, lint
from wrally.errors import PreflightError
from wrally.rules import core_len

# 《提灯人》项目文件里的两组词，用来测否定写法的识别
LIGHT = ("阳光照进", "光束", "阴影退去", "体积光", "光柱")
FINE_MOTION = ("写字", "书写", "展开", "碰杯", "举杯相碰")


def give_candidates(ws, key, ids, picked=None, files=True):
    data = ws.load()
    e = next(x for x in data["shots"] if x["key"] == key)
    e["keyframe"] = {"candidates": ids} | ({"asset_id": picked} if picked else {})
    ws.save(data)
    if files:
        folder = ws.state_dir / "keyframes" / key
        folder.mkdir(parents=True, exist_ok=True)
        for i, _ in enumerate(ids):
            buf = io.BytesIO()
            Image.new("RGB", (160, 90), (40 * i, 80, 120)).save(buf, "PNG")
            (folder / f"{keyframe.letter(i)}.png").write_bytes(buf.getvalue())


def test_pick_by_letter_resolves_asset_id(ws):
    give_candidates(ws, "01", ["i1", "i2", "i3"])
    rows = keyframe.pick(ws, [("01", "b")])
    assert rows == [("01", "B", "i2", "-", "")]
    assert dict(entry(ws, "01")["keyframe"]) == {
        "candidates": ["i1", "i2", "i3"],
        "asset_id": "i2",
        "pick": "B",
    }


def test_pick_several_shots_at_once_and_accepts_unpadded_keys(ws):
    give_candidates(ws, "01", ["i1", "i2"])
    give_candidates(ws, "02", ["j1", "j2"], picked="j1")
    rows = keyframe.pick(ws, [("1", "A"), ("02", "B")])
    assert [(r[0], r[1], r[3]) for r in rows] == [("01", "A", "-"), ("02", "B", "A")]


def test_pick_by_asset_id_for_images_outside_candidates(ws):
    """网页上传或画布生成的图不在候选里，直接给 asset_id。"""
    give_candidates(ws, "01", ["i1"])
    rows = keyframe.pick(ws, [("01", "dablnb16l3")])
    assert rows[0][:3] == ("01", "dablnb16l3", "dablnb16l3")
    assert "pick" not in entry(ws, "01")["keyframe"]


def test_bad_pick_changes_nothing(ws):
    give_candidates(ws, "01", ["i1", "i2"])
    before = ws.manifest_path.read_bytes()
    with pytest.raises(PreflightError) as e:
        keyframe.pick(ws, [("01", "A"), ("01", "C")])
    assert e.value.code == "unknown_candidate" and "have A-B" in e.value.hint
    with pytest.raises(PreflightError) as e:
        keyframe.pick(ws, [("99", "A")])
    assert e.value.code == "no_candidates" and "wrally fetch keyframe --shots 99" in e.value.hint
    with pytest.raises(PreflightError):
        keyframe.pick(ws, [("42", "A")])
    assert ws.manifest_path.read_bytes() == before


def test_repick_warns_that_the_video_is_stale(ws):
    give_candidates(ws, "01", ["i1", "i2"], picked="i1")
    data = ws.load()
    next(e for e in data["shots"] if e["key"] == "01")["video"] = {"status": "done"}
    ws.save(data)
    assert keyframe.pick(ws, [("01", "B")])[0][4] == "video was made with the previous pick"
    assert keyframe.pick(ws, [("01", "B")])[0][4] == ""  # 没换图就不提示


def test_sheet_writes_one_comparison_image_per_shot(ws):
    give_candidates(ws, "01", ["i1", "i2", "i3", "i4"], picked="i3")
    give_candidates(ws, "99", ["k1"], files=False)
    rows, made = keyframe.sheet(ws)
    by_key = {r[0]: r for r in rows}
    assert made == 1 and by_key["01"] == ("01", "4", "C", ".wrally/sheets/keyframes-01.jpg")
    assert by_key["99"][3] == "none (run: wrally fetch keyframe --shots 99)" and by_key["00"][3] == "-"
    with Image.open(ws.root / ".wrally/sheets/keyframes-01.jpg") as im:
        assert im.width == 3 * 480 + 4 * 8 and im.height == 2 * 270 + 3 * 8  # 3 列，4 张排两行


def test_sheet_includes_pick_from_outside_candidates(ws):
    give_candidates(ws, "02", ["i1"], picked="key02")
    (ws.state_dir / "keyframes" / "02" / "pick-key02.png").write_bytes(
        (ws.state_dir / "keyframes" / "02" / "A.png").read_bytes()
    )
    rows, _ = keyframe.sheet(ws, "02")
    assert rows == [("02", "1", "file", ".wrally/sheets/keyframes-02.jpg")]
    with Image.open(ws.root / rows[0][3]) as im:
        assert im.width == 2 * 480 + 3 * 8


def test_narration_length_does_not_count_punctuation():
    assert core_len("门后是雅典。公元前五世纪，") == 11
    assert core_len("《沉思录》") == 5  # 书名号照常计数


def test_lint_checks_nothing_extra_without_rules(ws):
    """wrally 没有自带的阈值和词表：项目没写 rules，就不查字数和用词。"""
    ws.write_text("prompts/01.anim.md", "皇帝提笔书写，一道光束照在卷轴上\n")
    set_field(ws, "02", narration="字" * 80)
    assert not any("words" in r[2] or r[2].startswith("narration:") for r in lint(ws).rows)


@pytest.mark.parametrize(("text", "expected"), [
    ("阳光照进房间，尘埃飞舞", ["阳光照进"]),
    ("不出现立体感、体积光、光束、光线渐变或镜头光晕。只有轻微的动态：海面起伏", []),
    ("画面中始终不出现太阳和光柱。", []),
    ("一道光束扫过墙面，不出现体积光", ["光束"]),
])  # fmt: skip
def test_light_words_ignore_negated_lists(text, expected):
    assert find_words(text, LIGHT) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("四位朋友举杯相碰，笑着喝酒", ["举杯相碰"]),
    ("杯子停在原处不动，不碰杯、不喝", []),
    ("执笔的手和卷轴始终不动，不书写，卷轴不展开也不卷起", []),
    ("皇帝提笔书写，卷轴缓缓展开", ["书写", "展开"]),
    ("手不动。随后他开始写字", ["写字"]),
])  # fmt: skip
def test_fine_motion_words_ignore_direct_negation(text, expected):
    """EP3 的 05、09 镜正是用「不碰杯」「不书写」来规避这些动作，不能反过来报警。"""
    assert find_words(text, FINE_MOTION) == expected


def set_field(ws, key, **fields):
    data = ws.load()
    next(e for e in data["shots"] if e["key"] == key).update(fields)
    ws.save(data)


def test_lint_flags_risky_animation_and_long_narration(ws):
    data = ws.load()
    data["rules"] = {
        "narration_max": 30,
        "words": {"light effect": {"words": list(LIGHT)}, "fine motion": {"words": list(FINE_MOTION)}},
    }
    ws.save(data)
    ws.write_text("prompts/01.anim.md", "皇帝提笔书写，一道光束照在卷轴上\n")
    set_field(ws, "01", narration="罗马皇帝马可奥勒留在军帐里写下沉思录提醒自己保持平静。")
    set_field(ws, "02", narration="字" * 14 + "，" + "字" * 14 + "，" + "字" * 3 + "。")
    report = lint(ws)
    problems = {(r[0], r[2]) for r in report.rows}
    assert ("01", "anim: light effect words 光束") in problems
    assert ("01", "anim: fine motion words 书写") in problems
    assert ("02", "narration: 31 chars > 30") in problems
    assert not any(p.startswith("subtitle") for _, p in problems)  # 逐条字幕的长短由合成脚本查
    assert report.errors == 0
    assert "| 01 | warn | anim: light effect words 光束 |" in (ws.state_dir / "lint.md").read_text(
        encoding="utf-8"
    )


def test_lint_reports_missing_prompt_file_as_error(ws):
    (ws.root / "prompts" / "02.image.md").unlink()
    report = lint(ws, "02")
    assert report.rows[0] == ("02", "error", "image: file missing prompts/02.image.md") and report.errors == 1


def test_lint_reads_audio_from_file_when_there_is_no_narration_field(ws):
    """没有配置旁白模板的工作区，旁白存在 prompts/<镜号>.audio.md 里，不算缺台词。"""
    assert not any("narration" in r[2] for r in lint(ws).rows)
    set_field(ws, "01", audio_prompt=None)
    assert ("01", "warn", "narration: missing") in lint(ws).rows


def test_lint_word_lists_can_be_changed_per_show(ws):
    ws.write_text("prompts/01.anim.md", "皇帝提笔书写，一道光束照在卷轴上\n")
    ws.write_text("prompts/01.image.md", "皇帝的手部特写，画面里没有水印\n")
    data = ws.load()
    data["rules"] = {
        "narration_max": None,
        "words": {
            "light effect": None,  # 关掉一组
            "fine motion": {"words": ["提笔"]},  # 换词表，kind 沿用缺省的 anim
            "hard to draw": {"kind": "image", "words": ["手部特写", "水印"]},  # 加一组查关键帧的
        },
    }
    ws.save(data)
    set_field(ws, "02", narration="字" * 14 + "，" + "字" * 14 + "，" + "字" * 3 + "。")
    problems = [(r[0], r[2]) for r in lint(ws).rows]
    assert ("01", "anim: fine motion words 提笔") in problems
    assert ("01", "image: hard to draw words 手部特写") in problems  # 「没有水印」是否定写法
    assert not any("light effect" in p or p.startswith("narration:") for _, p in problems)
