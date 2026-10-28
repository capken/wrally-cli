import json

import pytest
from conftest import entry

from wrally.commands.audio import gen, needs_redo
from wrally.errors import NetworkError, PreflightError
from wrally.rules import Rules

TEMPLATE = "使用<{voice_name}>的音色。他说：「{narration}」"


def edit(ws, key=None, **fields):
    data = ws.load()
    target = data if key is None else next(e for e in data["shots"] if e["key"] == key)
    target.update(fields)
    ws.save(data)


def extra(remote, story_id):
    return json.loads(next(s for s in remote.stories if s["story_id"] == story_id)["extra"])


@pytest.fixture
def ready(ws, remote):
    """旁白走模板：清单里只有台词。01、02 两镜都已绑定音色 voice1。"""
    ws.write_text("styles/n.md", TEMPLATE + "\n")
    edit(ws, defaults={"audio": {"model": "aud1", "voice_asset": "voice1", "voice_name": "旁白男声"},
                       "narration_template": "styles/n.md"})  # fmt: skip
    for key in ("01", "02"):
        edit(ws, key, audio_prompt=None, narration=f"台词{key}")
    remote.add_asset("voice1", "audio", b"voice", "ref.wav")
    remote.add_asset("voice2", "audio", b"voice", "ref2.wav")
    return ws, remote


async def run(ws, remote, spec="02", **kw):
    return await gen(ws, remote, spec, http=remote.http(), **kw)


async def test_gen_writes_prompt_then_model_then_submits(ready):
    """调用顺序：edit_content 写 audio_prompt → set_model audio → 提交。音色已绑定时不重复绑定。"""
    ws, remote = ready
    report = await run(ws, remote, "01,02")
    assert report.summary == "submitted 2 audio tasks  (01 02)"
    assert remote.actions() == ["edit_content", "set_model"]
    assert remote.count("shotlist_generate_audio") == 1 and remote.submissions == [["s01", "s02"]]
    assert extra(remote, "s02")["audio_prompt"] == "使用<旁白男声>的音色。他说：「台词02」"
    assert json.loads(extra(remote, "s02")["gen_config"])["audio"] == {
        "model": "aud1", "count": 1, "modelCapabilities": [14],
    }  # fmt: skip
    audio = entry(ws, "02")["audio"]
    assert (audio["status"], audio["task"], audio["results_before"]) == ("running", "task-s02", 0)
    order = [
        a.get("action") or t for t, a in remote.calls if t in ("shotlist_manage", "shotlist_generate_audio")
    ]
    assert order == ["list", "edit_content", "set_model", "list", "shotlist_generate_audio"]


async def test_voice_is_bound_per_shot_before_the_prompt_is_written(ready):
    ws, remote = ready
    edit(
        ws,
        defaults=ws.load()["defaults"]
        | {"audio": {"model": "aud1", "voice_asset": "voice2", "voice_name": "新音色"}},
    )
    await run(ws, remote, "01,02")
    assert remote.actions() == ["bind_asset", "bind_asset", "edit_content", "set_model"]
    [bound] = json.loads(extra(remote, "s02")["audio_role_data_json"])
    assert bound == {
        "asset_id": "voice2",
        "role_id": "voice2",
        "name": "新音色",
        "url": "https://dl.example.com/voice2",
    }
    assert "<新音色>" in extra(remote, "s02")["audio_prompt"]  # 提示词里的名字与绑定的名字一致


async def test_rerun_does_not_submit_again(ready):
    ws, remote = ready
    await run(ws, remote)
    remote.calls.clear()
    again = await run(ws, remote)
    assert again.rows == [("02", "skipped", "running, inputs unchanged")] and remote.calls == []


async def test_changed_narration_is_regenerated(ready):
    ws, remote = ready
    await run(ws, remote)
    remote.results[("s02", "audio")]["doing"] = 0
    remote.add_result("s02", "audio", "a1", "2026-10-06 12:00:00")
    edit(ws, "02", narration="改过的台词", audio=entry(ws, "02")["audio"] | {"status": "done"})
    report = await run(ws, remote)
    assert report.submitted == ["02"] and len(remote.submissions) == 2
    assert extra(remote, "s02")["audio_prompt"].endswith("「改过的台词」")
    assert entry(ws, "02")["audio"]["results_before"] == 1


async def test_audio_made_outside_wrally_is_kept_unless_redo(ready):
    ws, remote = ready
    edit(ws, "02", audio={"status": "done", "asset_id": "old", "file": ".wrally/audio/02.wav", "f0": 86})
    assert (await run(ws, remote)).rows[0][:2] == ("02", "skipped") and remote.calls == []
    assert (await run(ws, remote, redo=True)).submitted == ["02"]


async def test_redo_failed_only_redoes_high_pitch_and_failed_shots(ready):
    """旁白音高超过 rules.f0_max 的镜头，--redo-failed 一键重做。"""
    ws, remote = ready
    data = ws.load()
    data["rules"] = {"f0_max": 97}
    ws.save(data)
    rules = Rules(f0_max=97)
    edit(ws, "01", audio={"status": "done", "f0": 114, "file": ".wrally/audio/01.wav"})
    edit(ws, "02", audio={"status": "done", "f0": 86, "file": ".wrally/audio/02.wav"})
    assert needs_redo(entry(ws, "01"), rules) == "f0 114Hz > 97" and needs_redo(entry(ws, "02"), rules) == ""
    dry = await run(ws, remote, "01,02", redo_failed=True, dry_run=True)
    assert ("01", "would submit", "f0 114Hz > 97") in dry.rows and remote.calls == []
    report = await run(ws, remote, "01,02", redo_failed=True)
    assert report.submitted == ["01"] and remote.submissions == [["s01"]]
    edit(ws, "02", audio={"status": "failed"})
    assert needs_redo(entry(ws, "02"), rules) == "last run failed"
    assert needs_redo(entry(ws, "01"), Rules()) == ""  # 没设上限就不按音高重做


def test_redo_failed_also_covers_one_sided_audio():
    """只有一个声道有声音的旁白（check 写进清单的 channel_gap）也算没过检查。"""
    rules = Rules(f0_max=97, channel_gap_max=6)
    lopsided = {"audio": {"status": "done", "f0": 91, "channel_gap": 42.3}}
    assert needs_redo(lopsided, rules) == "channel gap 42.3dB > 6"
    assert needs_redo({"audio": {"status": "done", "f0": 91, "channel_gap": 0.1}}, rules) == ""
    assert needs_redo(lopsided, Rules(f0_max=97)) == ""  # 没设上限就不按声道重做


def test_redo_failed_also_covers_background_noise():
    rules = Rules(noise_floor_max=-50)
    noisy = {"audio": {"status": "done", "noise_floor": -44.2}}
    assert needs_redo(noisy, rules) == "noise floor -44.2dB > -50"
    assert needs_redo({"audio": {"status": "done", "noise_floor": -69.1}}, rules) == ""


async def test_missing_narration_or_defaults_stops_before_remote(ready):
    ws, remote = ready
    edit(ws, "02", narration=None)
    with pytest.raises(PreflightError) as e:
        await run(ws, remote, "01,02")
    assert e.value.code == "missing_narration" and "(02)" in e.value.hint and remote.calls == []
    edit(ws, defaults={"audio": {"model": "aud1"}})
    with pytest.raises(PreflightError) as e:
        await run(ws, remote, "01")
    assert e.value.code == "missing_defaults" and "voice_asset" in e.value.hint


async def test_interrupted_submit_is_not_repeated(ready):
    ws, remote = ready
    remote.submit_outcome = "timeout"
    with pytest.raises(NetworkError):
        await run(ws, remote)
    remote.submit_outcome = "ok"
    report = await run(ws, remote)
    assert report.rows == [("02", "skipped", "already submitted by an interrupted run")]
    assert len(remote.submissions) == 1


async def test_unbindable_voice_fails_those_shots_without_submitting(ready):
    ws, remote = ready
    edit(
        ws,
        defaults=ws.load()["defaults"]
        | {"audio": {"model": "aud1", "voice_asset": "ghost", "voice_name": "无"}},
    )
    report = await run(ws, remote)
    assert report.rows[0][:2] == ("02", "error: voice_asset_not_found") and report.failed == 1
    assert remote.submissions == [] and "edit_content" not in remote.actions()


async def test_audio_model_survives_even_if_writing_the_prompt_wipes_config(ready):
    """edit_content 必须在 set_model 之前：写旁白可能冲掉同字段里的模型配置，随后的 set_model 把它补回来。"""
    ws, remote = ready
    remote.audio_edit_clobbers_config = True
    report = await run(ws, remote)
    assert report.submitted == ["02"]
    assert json.loads(extra(remote, "s02")["gen_config"])["audio"]["model"] == "aud1"
    assert report.notes == ["note: video config changed on 02 (video gen rewrites it)"]
