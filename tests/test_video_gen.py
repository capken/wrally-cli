import io
import json

import pytest
from conftest import entry, set_web_copy
from PIL import Image

from wrally.commands.push import push
from wrally.commands.video import gen
from wrally.commands.wait import wait
from wrally.errors import NetworkError, PreflightError


def png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (96, 54), color).save(buf, "PNG")
    return buf.getvalue()


def edit(ws, key=None, **fields):
    data = ws.load()
    target = data if key is None else next(e for e in data["shots"] if e["key"] == key)
    target.update(fields)
    ws.save(data)


@pytest.fixture
def ready(ws, remote):
    """02 镜已选用 key02（远端 singleAssets 也是它），清单有视频默认值。"""
    edit(ws, defaults={"video": {"model": "vid1", "resolution": 1, "duration": 8, "audio": False}})
    remote.add_asset("key02", "image", png((200, 30, 30)), "02.png")
    return ws, remote


def video_config(remote, story_id="s02"):
    story = next(s for s in remote.stories if s["story_id"] == story_id)
    return json.loads(json.loads(story["extra"])["gen_config"])["video"]


async def run(ws, remote, spec="02", **kw):
    return await gen(ws, remote, spec, http=remote.http(), **kw)


async def test_gen_writes_single_image_config_verifies_then_submits_once(ready):
    ws, remote = ready
    report = await run(ws, remote)
    assert report.summary == "submitted 1 video tasks  (02)"
    assert report.rows == [("02", "submitted", "task-s02")]
    # 配置先写、再回读、最后才提交
    order = [
        a.get("action") or t for t, a in remote.calls if t != "shotlist_get_result" and t != "asset_detail"
    ]
    assert order == ["list", "set_model", "list", "shotlist_generate_video"]
    assert video_config(remote) == {
        "mode": "Text", "extraMode": None, "model": "vid1", "textModel": "vid1",
        "singleAssets": [{"asset_id": "key02", "assetId": "key02", "fileType": "image", "name": "02.png",
                          "url": "https://dl.example.com/key02"}],
        "aspectRatio": "16:9", "duration": 8, "resolution": 1, "generateAudio": False,
    }  # fmt: skip
    video = entry(ws, "02")["video"]
    assert (video["status"], video["task"], video["results_before"], video["failed_before"]) == (
        "running",
        "task-s02",
        0,
        0,
    )
    assert len(video["made_from"]) == 12 and entry(ws, "02")["keyframe"]["synced"] == "key02"
    assert remote.submissions == [["s02"]]


async def test_rerun_does_not_submit_again_or_even_call_remote(ready):
    """中途终止后重跑同一条命令，不产生重复的生成任务。"""
    ws, remote = ready
    await run(ws, remote)
    remote.calls.clear()
    again = await run(ws, remote)
    assert again.rows == [("02", "skipped", "running, inputs unchanged")] and again.submitted == []
    assert remote.calls == [] and len(remote.submissions) == 1


async def test_changed_prompt_must_be_pushed_first(ready):
    ws, remote = ready
    ws.write_text("prompts/02.anim.md", "改过的动画\n")
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "unpushed_prompts" and e.value.exit_code == 2
    assert remote.actions() == [] and remote.submissions == []


async def test_changed_inputs_are_regenerated_after_the_previous_run_is_done(ready):
    ws, remote = ready
    await run(ws, remote)
    remote.finish("s02", "v1")
    edit(ws, "02", video=entry(ws, "02")["video"] | {"status": "done"})
    ws.write_text("prompts/02.anim.md", "改过的动画\n")
    remote.stories[2]["animation_prompt"] = "改过的动画"  # 已经 push
    report = await run(ws, remote)
    assert report.submitted == ["02"] and len(remote.submissions) == 2
    assert entry(ws, "02")["video"]["results_before"] == 1


async def test_missing_pick_stops_before_any_remote_call(ready):
    """singleAssets 必须带 asset_id：缺选图直接报错，不提交任何任务。"""
    ws, remote = ready
    with pytest.raises(PreflightError) as e:
        await run(ws, remote, "01,02")
    assert e.value.code == "missing_asset_id" and "(01;" in e.value.hint and remote.calls == []


async def test_video_made_outside_wrally_is_not_regenerated_unless_redo(ready):
    ws, remote = ready
    edit(ws, "02", video={"status": "done", "asset_id": "old", "file": ".wrally/video/02.mp4"})
    report = await run(ws, remote)
    assert report.rows[0][:2] == ("02", "skipped") and remote.calls == []
    assert (await run(ws, remote, redo=True)).submitted == ["02"]


async def test_web_selection_that_differs_from_manifest_aborts(ready):
    """EP3 的 07/08 错位：清单记的图和网页选用的不是同一张，不能悄悄覆盖。"""
    ws, remote = ready
    remote.add_asset("mine", "image", png((30, 30, 200)), "02.png")
    edit(ws, "02", keyframe={"asset_id": "mine"})
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "pick_conflict" and "wrally verify-picks" in e.value.hint
    assert remote.actions() == [] and remote.submissions == []
    assert (await run(ws, remote, force_picks=True)).submitted == ["02"]


async def test_same_picture_under_a_new_id_is_not_a_conflict(ready):
    ws, remote = ready
    remote.add_asset("mine", "image", png((200, 30, 30)), "02.png")
    edit(ws, "02", keyframe={"asset_id": "mine"})
    assert (await run(ws, remote)).submitted == ["02"]
    assert video_config(remote)["singleAssets"][0]["asset_id"] == "mine"


async def test_repick_through_wrally_overwrites_what_wrally_wrote_before(ready):
    ws, remote = ready
    await run(ws, remote)
    remote.finish("s02", "v1")
    remote.add_asset("other", "image", png((30, 200, 30)), "02.png")
    edit(ws, "02", keyframe={"asset_id": "other", "synced": "key02"},
         video=entry(ws, "02")["video"] | {"status": "done"})  # fmt: skip
    assert (await run(ws, remote)).submitted == ["02"]
    assert video_config(remote)["singleAssets"][0]["asset_id"] == "other"


async def test_interrupted_submit_that_reached_the_server_is_not_repeated(ready):
    ws, remote = ready
    remote.submit_outcome = "timeout"
    with pytest.raises(NetworkError):
        await run(ws, remote)
    assert entry(ws, "02")["video"]["status"] == "submitting" and len(remote.submissions) == 1
    remote.submit_outcome = "ok"
    report = await run(ws, remote)
    assert report.rows == [("02", "skipped", "already submitted by an interrupted run")]
    assert len(remote.submissions) == 1 and entry(ws, "02")["video"]["status"] == "running"


async def test_interrupted_submit_that_never_arrived_is_retried_on_rerun(ready):
    ws, remote = ready
    remote.submit_outcome = "lost"
    with pytest.raises(NetworkError):
        await run(ws, remote)
    remote.submit_outcome = "ok"
    assert (await run(ws, remote)).submitted == ["02"] and remote.submissions == [["s02"]]


async def test_one_bad_shot_does_not_block_the_others(ready):
    ws, remote = ready
    edit(ws, "01", keyframe={"asset_id": "ghost"})
    report = await run(ws, remote, "01,02")
    assert report.submitted == ["02"] and report.failed == 1
    assert report.rows[0][:2] == ("01", "error: asset_not_found")
    assert remote.submissions == [["s02"]] and "video" not in entry(ws, "01")


async def test_shot_skipped_by_server_is_reported_as_failed(ready):
    ws, remote = ready
    remote.server_skips.add("s02")
    report = await run(ws, remote)
    assert report.submitted == [] and report.failed == 1
    assert report.rows[0][:2] == ("02", "error: skipped_by_server")
    assert entry(ws, "02")["video"]["status"] == "failed"


async def test_wait_keeps_waiting_for_a_task_not_yet_visible_as_doing(ready):
    """刚提交时远端可能还没把任务算进 doing，不能把旧结果当成完成。"""
    ws, remote = ready
    remote.add_result("s02", "video", "old", "2026-10-05 18:00:00")
    await run(ws, remote, redo=True)
    remote.results[("s02", "video")]["doing"] = 0  # 模拟任务还没显示出来
    now, sleeps = [0.0], []

    async def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) == 2:
            remote.add_result("s02", "video", "new", "2026-10-06 12:00:00")

    report = await wait(ws, remote, "video", "02", sleep=sleep, clock=lambda: now[0])
    assert sleeps == [120, 30] and report.rows == [("02", "done", "")]


async def test_wait_reports_failure_against_the_baseline_recorded_at_submit(ready):
    ws, remote = ready
    remote.add_result("s02", "video", "old", "2026-10-05 18:00:00")
    await run(ws, remote, redo=True)
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds
        if remote.results[("s02", "video")]["doing"]:
            remote.finish("s02", "", failed=True)

    report = await wait(ws, remote, "video", "02", sleep=sleep, clock=lambda: now[0])
    assert report.rows == [("02", "failed", "")] and report.failed == 1


async def test_dry_run_does_not_call_remote_or_touch_the_manifest(ready):
    ws, remote = ready
    before = ws.manifest_path.read_bytes()
    report = await run(ws, remote, dry_run=True)
    assert report.summary == "dry run: would submit 1 video tasks  (02)"
    assert remote.calls == [] and ws.manifest_path.read_bytes() == before


def story(remote, story_id="s02"):
    return next(s for s in remote.stories if s["story_id"] == story_id)


async def test_stale_web_copy_of_the_anim_prompt_stops_before_any_write(ready):
    """EP1 的 06 镜：网页上保存过的镜头另有一份动画提示词，生视频用的是它；两次视频都按旧动效做了。"""
    ws, remote = ready
    set_web_copy(story(remote), "网页上的旧动画", "animation_prompt_json")
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "stale_web_prompt" and "02" in str(e.value) and e.value.exit_code == 2
    assert remote.actions() == [] and remote.submissions == []
    # 网页上粘成一致之后可以提交
    set_web_copy(story(remote), story(remote)["animation_prompt"], "animation_prompt_json")
    assert (await run(ws, remote)).submitted == ["02"]


async def test_push_cannot_update_the_web_copy_of_the_anim_prompt_and_says_so(ready):
    ws, remote = ready
    set_web_copy(story(remote), story(remote)["animation_prompt"], "animation_prompt_json")
    ws.write_text("prompts/02.anim.md", "改过的动画\n")
    for dry_run in (True, False):
        report = await push(ws, remote, "prompts", "02", dry_run=dry_run)
        assert any(n.startswith("note: 02:") and "anim prompt on the web" in n for n in report.notes)
    assert story(remote)["animation_prompt"] == "改过的动画"
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "stale_web_prompt" and remote.submissions == []


async def test_per_shot_duration_overrides_the_default_and_counts_as_an_input(ready):
    """EP1 的 09 镜旁白 12 秒：只给这一镜 10 秒，其他镜头的指纹不受影响。"""
    ws, remote = ready
    await run(ws, remote)
    before = entry(ws, "02")["video"]["made_from"]
    remote.finish("s02", "v1")
    edit(ws, "02", video=entry(ws, "02")["video"] | {"status": "done"})
    assert (await run(ws, remote)).submitted == []  # 没写 video_duration：输入没变
    edit(ws, "02", video_duration=10)
    report = await run(ws, remote)
    assert report.submitted == ["02"] and video_config(remote)["duration"] == 10
    assert entry(ws, "02")["video"]["made_from"] != before


async def test_per_shot_resolution_overrides_the_default_and_counts_as_an_input(ready):
    """某一镜画面细节密，只给这一镜 720p：其他镜头的指纹不受影响。"""
    ws, remote = ready
    await run(ws, remote)
    before = entry(ws, "02")["video"]["made_from"]
    remote.finish("s02", "v1")
    edit(ws, "02", video=entry(ws, "02")["video"] | {"status": "done"})
    edit(ws, "02", video_resolution=3)
    report = await run(ws, remote)
    assert report.submitted == ["02"] and video_config(remote)["resolution"] == 3
    assert entry(ws, "02")["video"]["made_from"] != before


@pytest.mark.parametrize("bad", [0, -1, "3", 1.5, True])
async def test_bad_per_shot_resolution_stops_before_any_remote_call(ready, bad):
    ws, remote = ready
    edit(ws, "02", video_resolution=bad)
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "bad_video_resolution" and remote.calls == []


@pytest.mark.parametrize("bad", [0, -5, "10", 8.5, True])
async def test_bad_per_shot_duration_stops_before_any_remote_call(ready, bad):
    ws, remote = ready
    edit(ws, "02", video_duration=bad)
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "bad_video_duration" and remote.calls == []
