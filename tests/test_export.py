import json

import pytest
from typer.testing import CliRunner

from wrally import media
from wrally.cli import app
from wrally.commands.export import collect, export


def edit(ws, key=None, **fields):
    data = ws.load()
    target = data if key is None else next(e for e in data["shots"] if e["key"] == key)
    target.update(fields)
    ws.save(data)


def exported(ws) -> dict:
    return json.loads((ws.state_dir / "export.json").read_text(encoding="utf-8"))


@pytest.fixture
def cut(ws, monkeypatch):
    """四镜都有视频、旁白和台词。"""
    monkeypatch.setattr(media, "duration", lambda p: 8.0961 if p.suffix == ".mp4" else 3.5)
    for e in ws.load()["shots"]:
        key = e["key"]
        for name, ext in (("video", "mp4"), ("audio", "wav")):
            rel = f".wrally/{name}/{key}.{ext}"
            (ws.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws.root / rel).write_bytes(b"x")
            edit(ws, key, **{name: {"status": "done", "file": rel}})
        edit(ws, key, narration=f"台词{key}。")
    return ws


def test_every_shot_is_listed_in_order_with_relative_paths(cut):
    report = export(cut)
    assert report.summary == "exported 4 shots  -> .wrally/export.json  (updated)" and report.rows == []
    content = exported(cut)
    assert [s["key"] for s in content["shots"]] == ["00", "01", "02", "99"]
    assert content["shots"][2] == {
        "key": "02",
        "video": ".wrally/video/02.mp4",
        "video_seconds": 8.096,
        "audio": ".wrally/audio/02.wav",
        "audio_seconds": 3.5,
        "narration": "台词02。",
        "skip": [],
        "missing": [],
        "stale": [],
        "extra": {},
    }
    assert content["version"] == 1 and content["output"] == f"{cut.root.name}.mp4"
    assert content["project_dir"] is None and content["extra"] == {}
    assert "台词" in (cut.state_dir / "export.json").read_text(encoding="utf-8")  # 中文不转义


def test_export_is_idempotent_and_does_not_touch_the_manifest(cut):
    export(cut)
    manifest = cut.manifest_path.read_bytes()
    before = (cut.state_dir / "export.json").stat().st_mtime_ns
    again = export(cut)
    assert not again.changed and again.summary.endswith("(unchanged)")
    assert (cut.state_dir / "export.json").stat().st_mtime_ns == before
    assert cut.manifest_path.read_bytes() == manifest


def test_keys_wrally_does_not_know_are_passed_through_untouched(cut):
    """合成参数是合成脚本的事：wrally 不解释，也不解析里面的路径。"""
    edit(
        cut,
        output="EP 成片.mp4",
        assemble={"bgm": "assets/bgmA.wav", "titles": [{"text": "提 灯 人", "x": 640}]},
    )
    edit(cut, "02", assemble={"trim": 7.0, "video": "assets/02-裁头.mp4"}, skip=["assemble"])
    content = collect(cut, cut.load())
    assert content["output"] == "EP 成片.mp4"
    assert content["extra"] == {
        "assemble": {"bgm": "assets/bgmA.wav", "titles": [{"text": "提 灯 人", "x": 640}]}
    }
    shot = content["shots"][2]
    assert shot["extra"] == {"assemble": {"trim": 7.0, "video": "assets/02-裁头.mp4"}}
    assert shot["video"] == ".wrally/video/02.mp4" and shot["skip"] == [
        "assemble"
    ]  # 换不换、跳不跳由脚本决定
    # wrally 自己的小节不带出
    edit(cut, defaults={"border": "wave"}, rules={"f0_max": 200})
    assert list(collect(cut, cut.load())["extra"]) == ["assemble"]


def test_missing_material_is_marked_and_the_export_is_still_written(cut):
    (cut.root / ".wrally/video/01.mp4").unlink()
    edit(cut, "02", narration=None, audio={"status": "running"})
    report = export(cut)
    assert report.summary.startswith("exported 4 shots, 2 with missing or stale material  -> ")
    assert report.rows == [("01", "video", "-"), ("02", "audio,narration", "-")]
    shots = {s["key"]: s for s in exported(cut)["shots"]}
    assert shots["01"]["video"] == ".wrally/video/01.mp4" and shots["01"]["video_seconds"] is None
    assert shots["02"]["audio"] is None and shots["02"]["missing"] == ["audio", "narration"]


def test_stale_material_is_marked(cut):
    from wrally.commands import audio as audio_cmd

    edit(cut, defaults={"audio": {"model": "aud1", "voice_asset": "voice1"}})
    data = cut.load()
    target = next(e for e in data["shots"] if e["key"] == "02")
    target["audio"]["made_from"] = audio_cmd.fingerprint("别的台词", "aud1", "voice1")
    cut.save(data)
    assert export(cut).rows == [("02", "-", "audio was made from different narration")]
    assert exported(cut)["shots"][2]["stale"] == ["audio was made from different narration"]


def test_shots_can_be_selected(cut):
    assert [s["key"] for s in collect(cut, cut.load(), "01-02")["shots"]] == ["01", "02"]


def test_cli_prints_a_short_summary(cut):
    (cut.root / ".wrally/audio/99.wav").unlink()
    result = CliRunner().invoke(app, ["export", "-C", str(cut.root)])
    assert result.exit_code == 0  # 缺素材也照常导出，要不要紧由合成脚本判断
    lines = result.output.splitlines()
    assert lines[0].startswith("exported 4 shots, 1 with missing or stale material") and len(lines) <= 30
    assert any(line.split()[:2] == ["99", "audio"] for line in lines)
    assert "assemble" not in CliRunner().invoke(app, ["--help"]).output
