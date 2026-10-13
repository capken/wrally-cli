import json

import pytest
from conftest import entry, set_web_copy

from wrally.api.models import Shot
from wrally.commands.fetch import fetch
from wrally.commands.keyframe_gen import gen
from wrally.commands.lint import lint
from wrally.commands.push import push
from wrally.commands.wait import wait
from wrally.errors import NetworkError, PreflightError


def edit(ws, key=None, **fields):
    data = ws.load()
    target = data if key is None else next(e for e in data["shots"] if e["key"] == key)
    target.update(fields)
    ws.save(data)


@pytest.fixture
def ready(ws, remote):
    edit(ws, defaults={"image": {"model": "img9", "resolution": 4, "aspect": "16:9", "count": 2}})
    return ws, remote


def image_config(remote, story_id):
    story = next(s for s in remote.stories if s["story_id"] == story_id)
    return json.loads(json.loads(story["extra"])["gen_config"])["image"]


async def run(ws, remote, spec="01", **kw):
    return await gen(ws, remote, spec, http=remote.http(), **kw)


async def test_gen_sets_model_then_submits_all_shots_in_one_call(ready):
    ws, remote = ready
    report = await run(ws, remote, "00,01")
    assert report.summary == "submitted 2 keyframe tasks  (00 01)"
    order = [a.get("action") or t for t, a in remote.calls if t != "shotlist_get_result"]
    assert order == ["list", "set_model", "list", "shotlist_generate_image"]
    assert image_config(remote, "s01") == {
        "model": "img9",
        "count": 2,
        "aspectRatio": "16:9",
        "resolution": 4,
    }
    submit = next(a for t, a in remote.calls if t == "shotlist_generate_image")
    assert submit["story_ids"] == ["s00", "s01"] and submit["count"] == 2
    kf = entry(ws, "01")["keyframe"]
    assert kf["status"] == "running" and list(kf["tasks"]) == ["task-s01-0", "task-s01-1"]
    assert (kf["results_before"], kf["failed_before"]) == (0, 0) and len(kf["made_from"]) == 12


async def test_rerun_does_not_submit_again(ready):
    ws, remote = ready
    await run(ws, remote)
    remote.calls.clear()
    again = await run(ws, remote)
    assert again.submitted == [] and again.rows[0][:2] == ("01", "skipped") and remote.calls == []
    assert (await run(ws, remote, redo=True)).submitted == ["01"] and len(remote.submissions) == 2


async def test_shots_with_keyframes_made_outside_wrally_are_left_alone(ready):
    """02 镜已有选用图但不是 wrally 生成的：默认不花额度重做。"""
    ws, remote = ready
    report = await run(ws, remote, "02")
    assert report.rows == [
        ("02", "skipped", "has keyframes made outside wrally (--redo for more candidates)")
    ]
    assert remote.calls == []


async def test_shot_marked_skip_is_never_generated(ready):
    """片头沿用上一集的关键帧，--shots all 也不该给它生图。"""
    ws, remote = ready
    edit(ws, "00", skip=["keyframe"])
    report = await run(ws, remote, "00,01", redo=True)
    assert report.submitted == ["01"] and ("00", "skipped", "marked skip in episode.yaml") in report.rows
    assert remote.submissions == [["s01"]]


async def test_unpushed_prompt_stops_before_any_write(ready):
    ws, remote = ready
    ws.write_text("prompts/01.image.md", "改过的画面\n")
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "unpushed_prompts" and remote.actions() == [] and remote.submissions == []


async def test_interrupted_submit_is_not_repeated(ready):
    ws, remote = ready
    remote.submit_outcome = "timeout"
    with pytest.raises(NetworkError):
        await run(ws, remote)
    assert entry(ws, "01")["keyframe"]["status"] == "submitting"
    remote.submit_outcome = "ok"
    report = await run(ws, remote)
    assert report.rows == [("01", "skipped", "already submitted by an interrupted run")]
    assert len(remote.submissions) == 1


async def test_gen_wait_fetch_adds_new_candidates_after_existing_ones(ready):
    ws, remote = ready
    remote.add_result("s01", "image", "old1", "2026-10-05 14:00:00", b"old")
    await fetch(ws, remote, "keyframe", "01", http=remote.http())
    await run(ws, remote, redo=True)
    now, sleeps = [0.0], []

    async def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        if len(sleeps) == 1:
            remote.finish_image("s01", "new1", "2026-10-06 16:00:00", b"N1")
        if len(sleeps) == 2:
            remote.finish_image("s01", "new2", "2026-10-06 16:00:05", b"N2")

    report = await wait(ws, remote, "keyframe", "01", sleep=sleep, clock=lambda: now[0])
    assert sleeps == [40, 10] and report.rows == [("01", "done", "")]  # 图片先等 40 秒，之后每 10 秒
    await fetch(ws, remote, "keyframe", "01", http=remote.http())
    assert list(entry(ws, "01")["keyframe"]["candidates"]) == ["old1", "new1", "new2"]
    folder = ws.state_dir / "keyframes" / "01"
    assert [(folder / f"{x}.png").read_bytes() for x in "ABC"] == [b"old", b"N1", b"N2"]


@pytest.fixture
def recap(ready):
    """99 镜是回顾镜头，引用 02 镜的定稿关键帧。"""
    ws, remote = ready
    edit(ws, "99", keyframe={"refs": ["02"]})
    remote.add_asset("key02", "image", b"key", "02.png")
    return ws, remote


async def test_shot_with_refs_goes_through_canvas_with_the_picked_keyframes(recap):
    ws, remote = recap
    report = await run(ws, remote, "99")
    assert report.rows == [("99", "submitted", "2 images via canvas, refs 02")]
    [request] = remote.canvas_requests
    assert request["input_images"] == ["https://dl.example.com/key02"]  # 固定地址，不带签名
    assert request["prompt"] == "画面 片尾「马赛克」" and request["model"] == "img9"
    assert (request["count"], request["aspect_ratio"], request["resolution"]) == (2, "16:9", 4)
    assert request["short_series_project_id"] == "proj" and "project_id" not in request
    assert remote.count("shotlist_generate_image") == 0 and "set_model" not in remote.actions()
    kf = entry(ws, "99")["keyframe"]
    assert list(kf["canvas_tasks"]) == ["c1", "c2"] and kf["status"] == "running"


async def test_refs_need_the_referenced_shots_to_be_picked(recap):
    """回顾镜头要用定稿关键帧作参考图：被引用的镜头没定稿就不生成。"""
    ws, remote = recap
    edit(ws, "99", keyframe={"refs": ["01", "02"]})
    with pytest.raises(PreflightError) as e:
        await run(ws, remote, "99")
    assert e.value.code == "missing_ref_pick" and "needs picks of 01" in e.value.hint and remote.calls == []


async def test_canvas_results_become_candidates(recap):
    ws, remote = recap
    await run(ws, remote, "99")
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds
        remote.finish_canvas("c1", "cv1", b"one")
        remote.finish_canvas("c2", None)  # 两张里失败了一张

    report = await wait(ws, remote, "keyframe", "99", sleep=sleep, clock=lambda: now[0])
    kf = entry(ws, "99")["keyframe"]
    assert (
        report.rows == [("99", "done", "")]
        and list(kf["canvas_assets"]) == ["cv1"]
        and "canvas_tasks" not in kf
    )
    await fetch(ws, remote, "keyframe", "99", http=remote.http())
    assert list(entry(ws, "99")["keyframe"]["candidates"]) == ["cv1"]
    assert (ws.state_dir / "keyframes" / "99" / "A.png").read_bytes() == b"one"


async def test_all_canvas_tasks_failing_is_reported(recap):
    ws, remote = recap
    await run(ws, remote, "99")
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds
        remote.finish_canvas("c1", None)
        remote.finish_canvas("c2", None)

    report = await wait(ws, remote, "keyframe", "99", sleep=sleep, clock=lambda: now[0])
    assert report.rows == [("99", "failed", "")] and report.failed == 1


async def test_interrupted_canvas_submit_is_not_guessed(recap):
    ws, remote = recap
    edit(ws, "99", keyframe={"refs": ["02"]})
    await run(ws, remote, "99")
    kf = dict(entry(ws, "99")["keyframe"]) | {"status": "submitting"}
    kf.pop("canvas_tasks")
    edit(ws, "99", keyframe=kf)
    report = await run(ws, remote, "99")
    assert report.rows[0][:2] == ("99", "error: unknown_submit_state") and len(remote.canvas_requests) == 1


def test_lint_warns_when_refs_are_not_mentioned_in_the_prompt(recap):
    ws, _ = recap
    assert ("99", "warn", "image: has 1 refs but prompt never says 第N张图片") in lint(ws, "99").rows
    ws.write_text("prompts/99.image.md", "三联画，左格沿用第一张图片的人物\n")
    assert not any("refs" in r[2] for r in lint(ws, "99").rows)


async def test_dry_run_lists_what_would_be_submitted_without_calling_remote(ready):
    ws, remote = ready
    report = await run(ws, remote, "all", dry_run=True)
    assert report.summary == "dry run: would submit 3 keyframe tasks  (00 01 99), 1 skipped"
    assert remote.calls == [] and "status" not in (entry(ws, "01").get("keyframe") or {})


def story(remote, story_id):
    return next(s for s in remote.stories if s["story_id"] == story_id)


async def test_stale_web_copy_of_the_prompt_stops_before_any_write(ready):
    """在网页上编辑过的镜头另有一份提示词，生图用的是那一份；它和本地不一致时不提交。"""
    ws, remote = ready
    set_web_copy(story(remote, "s01"), "网页上的旧文本")
    with pytest.raises(PreflightError) as e:
        await run(ws, remote, "00,01")
    assert e.value.code == "stale_web_prompt" and "01" in str(e.value) and "00" not in str(e.value)
    assert remote.actions() == [] and remote.submissions == []
    # 网页上粘成一致之后可以提交
    set_web_copy(story(remote, "s01"), story(remote, "s01")["image_prompt"])
    assert (await run(ws, remote, "00,01")).submitted == ["00", "01"]


async def test_push_cannot_update_the_web_copy_and_says_so(ready):
    ws, remote = ready
    set_web_copy(story(remote, "s01"), story(remote, "s01")["image_prompt"])
    ws.write_text("prompts/01.image.md", "改过的画面\n")
    for dry_run in (True, False):
        report = await push(ws, remote, "prompts", dry_run=dry_run)
        assert any(n.startswith("note: 01:") and "paste the prompt on the web" in n for n in report.notes)
    assert Shot.from_story(story(remote, "s01")).image_prompt == "改过的画面"
    with pytest.raises(PreflightError) as e:
        await run(ws, remote)
    assert e.value.code == "stale_web_prompt" and remote.submissions == []
