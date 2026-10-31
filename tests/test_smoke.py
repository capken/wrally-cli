"""wrally smoke：依次生成图像、配音、5 秒视频并检查文件。"""

import json

import pytest
from conftest import VIDEO_MODEL

from wrally.commands import gen, smoke
from wrally.errors import PreflightError, ToolError

pytestmark = pytest.mark.anyio

PROJECT_FILE = """\
project_id: proj
defaults:
  image: {model: Seedream-5 Pro, aspect: "16:9"}
  video: {model: vid9, duration: 8}
  audio: {model: aud9, voice_asset: voiceA}
"""

GOOD = {
    "image": {"duration": 0.0, "width": 1024, "height": 576, "video": True, "audio": False},
    "audio": {"duration": 3.2, "width": None, "height": None, "video": False, "audio": True},
    "video": {"duration": 5.04, "width": 854, "height": 480, "video": True, "audio": False},
}


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "POLL_SECONDS", 0)
    monkeypatch.setitem(VIDEO_MODEL, "duration_options", [4, 5, 8])
    (tmp_path / "wrally.yaml").write_text(PROJECT_FILE, encoding="utf-8")
    probes = dict(GOOD)
    monkeypatch.setattr(smoke.media, "probe", lambda path: probes[path.stem.split("-")[0]])
    return probes


def autofinish(remote, outcomes: dict[str, str | None], hold: set[str] = frozenset()):
    """第一次查询任务时就让它结束。outcomes：任务 ID → 媒资 ID（None 表示失败）；hold 里的保持 running。"""
    original = remote.call

    def kind_of(task: str) -> str:  # 任务 c<n> 对应第 n 次提交
        req = remote.canvas_requests[int(task[1:]) - 1]
        return "video" if "duration" in req else ("audio" if req.get("mode") == "audio" else "image")

    async def call(tool, arguments, **kw):
        task = arguments.get("task_id")
        if tool == "canvas_get_task" and task not in hold and remote.canvas[task]["status"] == "running":
            remote.finish_canvas(task, outcomes.get(task, f"asset-{task}"), kind=kind_of(task))
        return await original(tool, arguments, **kw)

    remote.call = call


async def test_runs_three_steps_in_order_and_video_uses_image(tmp_path, remote):
    autofinish(remote, {})
    report = await smoke.run(remote, tmp_path, http=remote.http())
    assert [r[2] for r in report.rows] == ["done", "done", "done"]
    assert not report.failed and not report.pending
    image, audio, video = remote.canvas_requests
    assert image["model"] == "img9" and image["resolution"] == 4 and image["count"] == 1  # 最低分辨率
    assert audio["ref_audios"] == ["voiceA"]
    assert video["duration"] == 5 and video["enable_sound"] is False and video["resolution"] == 1
    assert "single_image_url" in video  # 接了图像那一步的结果
    assert all(r["short_series_project_id"] == "proj" for r in remote.canvas_requests)
    assert report.rows[2][4] == "5.04s, 854x480, no audio track"
    assert (report.run_dir / "video.mp4").exists()


async def test_dry_run_submits_nothing(tmp_path, remote):
    report = await smoke.run(remote, tmp_path, dry_run=True)
    assert [r[0] for r in report.rows] == ["image", "audio", "video"]
    assert not remote.canvas_requests and not (tmp_path / smoke.ROOT).exists()


async def test_no_5s_option_stops_before_any_submit(tmp_path, remote, monkeypatch):
    monkeypatch.setitem(VIDEO_MODEL, "duration_options", [4, 8])
    with pytest.raises(PreflightError) as e:
        await smoke.run(remote, tmp_path)
    assert e.value.code == "bad_duration" and not remote.canvas_requests


async def test_missing_model_is_preflight(tmp_path, remote):
    (tmp_path / "wrally.yaml").write_text("defaults: {}\n", encoding="utf-8")
    with pytest.raises(PreflightError) as e:
        await smoke.run(remote, tmp_path, only=["image"])
    assert e.value.code == "missing_model"
    report = await smoke.run(remote, tmp_path, models={"image": "img9"}, only=["image"], dry_run=True)
    assert report.rows[0][1] == "Seedream-5 Pro"


async def test_image_failure_falls_back_to_text_video(tmp_path, remote):
    autofinish(remote, {"c1": None})
    report = await smoke.run(remote, tmp_path, http=remote.http())
    assert [r[2] for r in report.rows] == ["failed", "done", "done"] and report.failed == 1
    assert "single_image_url" not in remote.canvas_requests[2]
    assert report.rows[2][4].endswith("text only")


async def test_server_rejection_does_not_stop_later_steps(tmp_path, remote, monkeypatch):
    autofinish(remote, {})
    real = gen.canvas.submit

    async def submit(client, request, **kw):
        if request.kind == "audio":
            raise ToolError("40001", tool="canvas_generate_audio")
        return await real(client, request, **kw)

    monkeypatch.setattr(gen.canvas, "submit", submit)
    report = await smoke.run(remote, tmp_path, http=remote.http())
    assert [r[2] for r in report.rows] == ["done", "failed: 40001", "done"]


async def test_timeout_then_resume_does_not_resubmit(tmp_path, remote):
    autofinish(remote, {}, hold={"c3"})
    first = await smoke.run(remote, tmp_path, timeout=0, http=remote.http())
    assert [r[2] for r in first.rows] == ["done", "done", "running"] and first.pending == 1
    assert "--resume" in first.summary

    autofinish(remote, {})
    again = await smoke.run(remote, tmp_path, resume=True, http=remote.http())
    assert [r[2] for r in again.rows] == ["done", "done", "done"] and not again.pending
    assert len(remote.canvas_requests) == 3  # 没有重复提交
    plan = json.loads((again.run_dir / smoke.PLAN).read_text(encoding="utf-8"))
    assert plan["steps"]["video"]["job_id"] == "003"


async def test_wrong_video_length_fails_check(tmp_path, remote, setup):
    setup["video"] = GOOD["video"] | {"duration": 8.0}
    autofinish(remote, {})
    report = await smoke.run(remote, tmp_path, http=remote.http())
    assert report.rows[2][2] == "failed: check" and "want 5s" in report.rows[2][4]


async def test_resume_without_run(tmp_path, remote):
    with pytest.raises(PreflightError) as e:
        await smoke.run(remote, tmp_path, resume=True)
    assert e.value.code == "no_run"


async def test_resume_resubmits_failed_step(tmp_path, remote):
    autofinish(remote, {"c2": None})  # 配音失败（远端繁忙）
    first = await smoke.run(remote, tmp_path, http=remote.http())
    assert [r[2] for r in first.rows] == ["done", "failed", "done"]

    autofinish(remote, {})
    again = await smoke.run(remote, tmp_path, resume=True, http=remote.http())
    assert [r[2] for r in again.rows] == ["done", "done", "done"]
    assert [r["mode"] for r in remote.canvas_requests[3:]] == ["audio"]  # 只重交了失败的那一步
