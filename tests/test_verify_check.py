import io

import pytest
from conftest import entry
from PIL import Image

from wrally import media
from wrally.commands.check import check
from wrally.commands.verify import verify_picks
from wrally.errors import PreflightError


def png(color, size=(96, 54), dot=None) -> bytes:
    im = Image.new("RGB", size, color)
    if dot:
        im.putpixel((1, 1), dot)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def set_pick(ws, key, asset_id):
    data = ws.load()
    next(e for e in data["shots"] if e["key"] == key)["keyframe"] = {"asset_id": asset_id}
    ws.save(data)


async def test_same_id_passes_without_downloading(ws, remote):
    report = await verify_picks(ws, remote, "all", http=remote.http())
    rows = {r[0]: r[3] for r in report.rows}
    assert rows == {"00": "no pick", "01": "no pick", "02": "ok", "99": "no pick"}
    assert remote.http_hits == [] and not report.mismatches


async def test_reuploaded_image_with_new_id_still_passes(ws, remote):
    """网页手工上传同一张图会产生新的 asset_id，要靠像素比对兜底。"""
    set_pick(ws, "02", "mine")
    remote.add_asset("mine", "image", png((200, 30, 30)), "02.png")
    remote.add_asset("key02", "image", png((200, 30, 30), size=(192, 108), dot=(190, 40, 40)), "02.png")
    report = await verify_picks(ws, remote, "02", http=remote.http())
    assert report.rows == [("02", "mine", "key02", "ok (same pixels)")] and not report.mismatches


async def test_swapped_pick_is_caught(ws, remote):
    """EP3 的 07/08 错位：清单记的图和网页选用的不是同一张。"""
    set_pick(ws, "02", "wrong")
    remote.add_asset("wrong", "image", png((30, 30, 200)), "08.png")
    remote.add_asset("key02", "image", png((200, 30, 30)), "02.png")
    report = await verify_picks(ws, remote, "all", http=remote.http())
    assert report.mismatches == ["02"]
    assert report.rows[0] == ("02", "wrong", "key02", "MISMATCH")  # 不一致的排最前


async def test_remote_without_single_assets_is_unset_not_mismatch(ws, remote):
    set_pick(ws, "01", "mine")
    report = await verify_picks(ws, remote, "01", http=remote.http())
    assert report.rows == [("01", "mine", "-", "unset")] and not report.mismatches


async def test_undownloadable_image_is_an_error_not_a_pass(ws, remote):
    set_pick(ws, "02", "ghost")
    remote.add_asset("key02", "image", png((1, 2, 3)), "02.png")
    report = await verify_picks(ws, remote, "02", http=remote.http())
    assert report.rows[0][3] == "error: asset_not_found" and report.errors == 1


def test_image_distance_separates_same_from_different(tmp_path):
    a, b, c = tmp_path / "a.png", tmp_path / "b.jpg", tmp_path / "c.png"
    a.write_bytes(png((120, 80, 40)))
    Image.new("RGB", (300, 200), (122, 79, 41)).save(b, "JPEG")
    c.write_bytes(png((40, 80, 120)))
    assert media.same_image(a, b) and not media.same_image(a, c)


@pytest.fixture
def local_media(ws, monkeypatch):
    """三镜都有视频和旁白；时长和音高由文件内容决定，不依赖 ffmpeg 和 librosa。"""
    spec = {"00": (8.0, 8.8, 81), "01": (8.0, 10.5, 81), "02": (8.0, 8.2, 114)}
    data = ws.load()
    for e in data["shots"]:
        if e["key"] in spec:
            v, a, f0 = spec[e["key"]]
            for name, ext, value in (("video", "mp4", v), ("audio", "wav", f"{a} {f0}")):
                rel = f".wrally/{name}/{e['key']}.{ext}"
                (ws.root / rel).parent.mkdir(parents=True, exist_ok=True)
                (ws.root / rel).write_text(str(value))
                e[name] = {"status": "done", "file": rel}
    next(e for e in data["shots"] if e["key"] == "00")["narration"] = "字" * 31
    data["rules"] = {"f0_max": 97, "slow_max": 1.25, "narration_max": 30}
    ws.save(data)
    calls = {"f0": 0, "sheet": 0}

    def sheet(video, out, **_):
        calls["sheet"] += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return True

    def f0(audio):
        calls["f0"] += 1
        return float(audio.read_text().split()[1])

    monkeypatch.setattr(media, "duration", lambda p: float(p.read_text().split()[0]))
    monkeypatch.setattr(media, "contact_sheet", sheet)
    monkeypatch.setattr(media, "median_f0", f0)
    return calls


def test_check_flags_pitch_slowdown_and_long_narration(ws, local_media):
    report = check(ws)
    flagged = {r[0]: r[5] for r in report.flagged()}
    assert flagged == {
        "00": "narration 31 chars > 30",
        "01": "slow x1.31 > 1.25",
        "02": "f0 114Hz > 97",
    }
    assert report.summary == "checked 4 shots: 3 sheets, 3 warnings  -> .wrally/report.md"
    assert entry(ws, "02")["audio"]["f0"] == 114
    text = (ws.state_dir / "report.md").read_text(encoding="utf-8")
    assert "| 01 | 8.0s | 10.5s | x1.31 | 81 | .wrally/sheets/01.jpg | slow x1.31 > 1.25 |" in text
    assert "| 99 | - | - | - | - | - | - |" in text


def test_check_reuses_sheets_and_cached_f0(ws, local_media):
    check(ws)
    check(ws)
    assert local_media == {"f0": 3, "sheet": 3}


def test_check_respects_shots_selector(ws, local_media):
    report = check(ws, "02")
    assert [r[0] for r in report.rows] == ["02"] and local_media["sheet"] == 1


def test_f0_parameters_are_fixed(monkeypatch, tmp_path):
    """各部片子的 rules.f0_max 是按这组参数定的，换参数会让同一条旁白差出几十赫兹。"""
    import sys
    import types

    import numpy as np

    seen = {}

    def load(path, sr):
        seen["load"] = sr
        return np.ones(16000), sr

    def pyin(y, **kwargs):
        seen["pyin"] = kwargs
        return np.array([np.nan, 80.0, 90.0, 100.0, np.nan]), None, None

    monkeypatch.setitem(sys.modules, "librosa", types.SimpleNamespace(load=load, pyin=pyin))
    assert media.median_f0(tmp_path / "a.wav") == 90.0  # 只统计有声帧
    assert seen == {"load": 16000, "pyin": {"fmin": 55, "fmax": 300, "sr": 16000, "frame_length": 1024}}


def test_check_flags_media_made_from_outdated_inputs(ws, local_media):
    """EP3 的 02、06、07、08：旁白念的是另一版台词，字幕和声音对不上。"""
    from wrally.commands import audio as audio_cmd
    from wrally.commands import video as video_cmd

    data = ws.load()
    data["defaults"] = {"audio": {"model": "aud1", "voice_asset": "voice1"}, "video": {"model": "vid1"}}
    shots = {e["key"]: e for e in data["shots"]}
    settings = video_cmd.video_settings(data["defaults"])
    for key in ("01", "02"):
        del shots[key]["audio_prompt"]
        shots[key]["narration"] = "原来的台词。"
        shots[key]["audio"]["made_from"] = audio_cmd.fingerprint("原来的台词。", "aud1", "voice1")
    shots["02"]["video"]["made_from"] = video_cmd.fingerprint("key02", "动画 02", settings)
    shots["01"]["narration"] = "改过的台词。"
    ws.save(data)
    flagged = {r[0]: r[5] for r in check(ws, "01,02").flagged()}
    assert "audio was made from different narration" in flagged["01"]
    assert "audio was made" not in flagged.get("02", "") and "video was made" not in flagged.get("02", "")

    data = ws.load()
    next(e for e in data["shots"] if e["key"] == "02")["keyframe"]["asset_id"] = "another"
    ws.save(data)
    assert (
        "video was made from a different keyframe or prompt"
        in {r[0]: r[5] for r in check(ws, "02").rows}["02"]
    )


def test_check_uses_the_per_shot_video_duration(ws, local_media):
    from wrally.commands import video as video_cmd

    data = ws.load()
    data["defaults"] = {"video": {"model": "vid1", "duration": 8}}
    shot = next(e for e in data["shots"] if e["key"] == "02")
    shot["video_duration"] = 10
    ten = video_cmd.shot_settings(video_cmd.video_settings(data["defaults"]), shot)
    assert ten.duration == 10
    shot["video"]["made_from"] = video_cmd.fingerprint("key02", "动画 02", ten)
    ws.save(data)
    assert "video was made" not in {r[0]: r[5] for r in check(ws, "02").rows}["02"]
    data = ws.load()
    del next(e for e in data["shots"] if e["key"] == "02")["video_duration"]
    ws.save(data)
    assert "video was made" in {r[0]: r[5] for r in check(ws, "02").rows}["02"]


def test_check_thresholds_come_from_rules(ws, local_media):
    data = ws.load()
    data["rules"] = {"f0_max": 200, "slow_max": None, "narration_max": 40}
    ws.save(data)
    report = check(ws)
    assert report.flagged() == [] and report.warnings == 0
    assert "音高 ≤ 200 Hz，放慢倍数 ≤ 不限，旁白 ≤ 40 字" in (ws.state_dir / "report.md").read_text(
        encoding="utf-8"
    )
    data = ws.load()
    data["rules"] = {"f0_max": 80, "slow_max": 1.25}
    ws.save(data)
    assert {r[0]: r[5] for r in check(ws).flagged()}["01"] == "f0 81Hz > 80; slow x1.31 > 1.25"


def test_nothing_is_flagged_and_pitch_is_not_measured_without_rules(ws, local_media):
    """wrally 没有自带的阈值：项目没写 rules 就只出抽帧图和时长。"""
    data = ws.load()
    del data["rules"]
    ws.save(data)
    report = check(ws)
    assert report.flagged() == [] and report.notes == [] and local_media == {"f0": 0, "sheet": 3}
    assert "audio" in entry(ws, "02") and "f0" not in entry(ws, "02")["audio"]
    assert "音高 ≤ 不限 Hz，放慢倍数 ≤ 不限，旁白 ≤ 不限 字" in (ws.state_dir / "report.md").read_text(
        encoding="utf-8"
    )


def test_pitch_is_skipped_with_a_note_when_librosa_is_not_installed(ws, local_media, monkeypatch):
    monkeypatch.setattr(media, "pitch_available", lambda: False)
    data = ws.load()
    next(e for e in data["shots"] if e["key"] == "01")["audio"]["f0"] = 120  # 以前量过的照常判断
    ws.save(data)
    report = check(ws)
    assert report.notes == [
        "note: pitch not checked, librosa is not installed (install wrally with the `pitch` extra)"
    ]
    assert local_media["f0"] == 0
    flagged = {r[0]: r[5] for r in report.flagged()}
    assert flagged["01"] == "f0 120Hz > 97; slow x1.31 > 1.25" and "02" not in flagged
    assert "pitch not checked" in (ws.state_dir / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("rules", "hint"),
    [
        ({"f0max": 90}, "unknown rules.f0max"),
        ({"slow_max": "fast"}, "rules.slow_max"),
        ({"f0_max": True}, "rules.f0_max"),
        ({"words": {"mine": {"kind": "audio", "words": ["x"]}}}, "rules.words.mine.kind"),
        ({"words": {"mine": {"words": "x"}}}, "rules.words.mine"),
        ({"words": ["光束"]}, "rules.words"),
    ],
)
def test_bad_rules_stop_before_anything_is_checked(ws, local_media, rules, hint):
    data = ws.load()
    data["rules"] = rules
    ws.save(data)
    with pytest.raises(PreflightError) as e:
        check(ws)
    assert e.value.code == "bad_rules" and hint in e.value.hint and local_media == {"f0": 0, "sheet": 0}
