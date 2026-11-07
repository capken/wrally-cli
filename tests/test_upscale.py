import pytest
from conftest import entry

from wrally import media
from wrally.commands import upscale
from wrally.commands.check import stale
from wrally.commands.export import collect
from wrally.errors import PreflightError


def edit(ws, key, **fields):
    data = ws.load()
    target = next(e for e in data["shots"] if e["key"] == key)
    target.update(fields)
    ws.save(data)


async def no_sleep(_seconds):
    return None


@pytest.fixture
def cut(ws, remote, monkeypatch):
    """02、01 有下载好的视频（vid02、vid01）；00、99 没有视频。"""
    monkeypatch.setattr(media, "duration", lambda p: 8.0)
    for key in ("01", "02"):
        rel = f".wrally/video/{key}.mp4"
        (ws.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws.root / rel).write_bytes(b"orig")
        edit(ws, key, video={"status": "done", "asset_id": f"vid{key}", "file": rel})
    return ws


def submitted_assets(remote) -> list[str]:
    return [a for t, args in remote.calls if t == "canvas_upscale_video" for a in args["asset_ids"]]


async def run(ws, remote, spec="02", **kw):
    return await upscale.upscale(ws, remote, spec, **kw)


async def test_upscales_the_video_in_the_manifest_then_downloads_and_export_uses_it(cut, remote):
    report = await run(cut, remote)
    assert report.submitted == ["02"] and report.label == "UpScale-02-v2 x2"
    req = remote.canvas_requests[0]
    assert req["asset_ids"] == ["vid02"] and req["model"] == "up2" and req["scale"] == "2"
    assert req["short_series_project_id"] == "proj" and "project_id" not in req  # 不建画布节点
    up = entry(cut, "02")["video"]["upscaled"]
    assert up["status"] == "running" and up["source"] == "vid02" and up["task"] == "u1"

    remote.finish_canvas("u1", "big02", content=b"big", kind="video")
    got = await upscale.collect(cut, remote, "02", http=remote.http(), sleep=no_sleep)
    assert got.rows == [("02", "done", ".wrally/video/02-x2.mp4")] and got.failed == 0
    assert (cut.root / ".wrally/video/02-x2.mp4").read_bytes() == b"big"
    video = entry(cut, "02")["video"]
    assert video["file"] == ".wrally/video/02.mp4"  # 原视频不动
    shot = next(s for s in collect(cut, cut.load())["shots"] if s["key"] == "02")
    assert shot["video"] == ".wrally/video/02-x2.mp4" and shot["video_upscale"] == "2"


async def test_same_inputs_are_not_resubmitted_but_another_scale_is(cut, remote):
    await run(cut, remote)
    again = await run(cut, remote)
    assert again.submitted == [] and again.rows == [("02", "skipped", "running, inputs unchanged")]
    assert len(remote.canvas_requests) == 1
    four = await run(cut, remote, scale="4", model="UpScale-01-v2")  # 名称不分大小写也行
    assert four.submitted == ["02"]
    assert remote.canvas_requests[-1]["scale"] == "4" and remote.canvas_requests[-1]["model"] == "up1"


async def test_defaults_upscale_picks_model_and_scale(cut, remote):
    data = cut.load()
    data["defaults"] = {"upscale": {"model": "up1", "scale": "4"}}
    cut.save(data)
    report = await run(cut, remote, dry_run=True)
    assert report.summary == "dry run: would upscale 1 videos with UpScale-01-v2 x4  (02)"
    assert remote.canvas_requests == []


@pytest.mark.parametrize(
    "kw, code",
    [({"scale": "3"}, "bad_scale"), ({"model": "nope"}, "unknown_model")],
)
async def test_bad_model_or_scale_stops_before_submitting(cut, remote, kw, code):
    with pytest.raises(PreflightError) as err:
        await run(cut, remote, **kw)
    assert err.value.code == code and remote.canvas_requests == []


async def test_shots_without_a_video_stop_everything(cut, remote):
    with pytest.raises(PreflightError) as err:
        await run(cut, remote, "all")
    assert err.value.code == "missing_video" and "00,99" in err.value.hint
    assert remote.canvas_requests == []


async def test_timeout_leaves_submitting_and_blocks_until_redo(cut, remote):
    remote.submit_outcome = "timeout"
    report = await run(cut, remote)
    assert report.failed == 1 and report.rows[0][1] == "error: network_error"
    assert entry(cut, "02")["video"]["upscaled"]["status"] == "submitting"
    remote.submit_outcome = "ok"
    blocked = await run(cut, remote)
    assert blocked.rows[0][1] == "error: pending_submit" and len(remote.canvas_requests) == 1
    redo = await run(cut, remote, redo=True)
    assert redo.submitted == ["02"] and len(remote.canvas_requests) == 2


async def test_server_not_submitting_an_unreadable_video_is_a_failure(cut, remote):
    remote.upscale_unreadable = {"vid02"}
    report = await run(cut, remote, "01-02")
    assert report.submitted == ["01"] and report.failed == 1
    assert ("02", "error: not_submitted") == report.rows[0][:2]
    assert entry(cut, "02")["video"]["upscaled"]["status"] == "failed"
    remote.upscale_unreadable = set()
    assert (await run(cut, remote)).submitted == ["02"]  # 失败的可以直接重跑


async def test_a_new_video_makes_the_upscale_stale_and_export_falls_back(cut, remote):
    await run(cut, remote)
    remote.finish_canvas("u1", "big02", kind="video")
    await upscale.collect(cut, remote, "02", http=remote.http(), sleep=no_sleep)
    data = cut.load()
    e = next(x for x in data["shots"] if x["key"] == "02")
    e["video"]["asset_id"] = "vid02-new"  # fetch video 换了一条
    cut.save(data)
    assert stale(cut, cut.load(), entry(cut, "02")) == ["upscale was made from an older video"]
    shot = next(s for s in collect(cut, cut.load())["shots"] if s["key"] == "02")
    assert shot["video"] == ".wrally/video/02.mp4" and shot["video_upscale"] is None
    again = await run(cut, remote)
    assert again.submitted == ["02"] and remote.canvas_requests[-1]["asset_ids"] == ["vid02-new"]


async def test_collect_reports_failures_and_timeouts(cut, remote):
    await run(cut, remote, "01-02")
    remote.finish_canvas("u1", None, error={"message": "busy"})
    clock = iter(range(0, 10_000, 100))
    got = await upscale.collect(
        cut, remote, "01-02", timeout=150, http=remote.http(), sleep=no_sleep, clock=lambda: next(clock)
    )
    assert got.failed == 1 and got.timed_out == 1
    assert dict((r[0], r[1]) for r in got.rows) == {"01": "failed", "02": "running"}
    assert entry(cut, "01")["video"]["upscaled"]["error"] == "busy"


async def test_video_still_being_made_is_skipped(cut, remote):
    edit(cut, "02", video={"status": "running", "asset_id": "vid02", "file": ".wrally/video/02.mp4"})
    report = await run(cut, remote)
    assert report.rows == [("02", "skipped", "a new video is still being made")]
    assert remote.canvas_requests == []


def test_cli_submits_then_rerun_waits_and_reports(cut, remote, monkeypatch):
    import anyio
    from typer.testing import CliRunner

    from wrally import cli

    monkeypatch.setattr(cli, "_remote", lambda verbose, work: anyio.run(work, remote))
    runner = CliRunner()
    args = ["video", "upscale", "--shots", "02", "-C", str(cut.root)]
    first = runner.invoke(cli.app, [*args, "--no-wait"])
    assert first.exit_code == 0, first.output
    assert first.output.startswith("submitted 1 upscale tasks with UpScale-02-v2 x2  (02)")
    remote.finish_canvas("u1", None, error="busy")
    second = runner.invoke(cli.app, args)  # 同样的输入不再提交，只接着等
    assert second.exit_code == 1 and len(remote.canvas_requests) == 1
    assert "waited 0s: 0 upscaled, 1 failed" in second.output and "busy" in second.output
