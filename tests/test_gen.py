"""wrally gen：不用项目和清单的单独生成。"""

import json

import pytest

from wrally.commands import gen
from wrally.errors import NetworkError, PreflightError, ToolError

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gen, "POLL_SECONDS", 0)


def log(out):
    return json.loads((out / gen.LOG).read_text(encoding="utf-8"))["jobs"]


def audio_params(**extra):
    base = {"prompt": "他说：「光有多快？」", "model": "seed-audio-1.5", "count": 1, "voices": ["voiceA"]}
    return base | extra


async def test_audio_submits_with_voice_and_downloads(tmp_path, remote):
    out = tmp_path / "out"
    report = await gen.gen(remote, "audio", audio_params(name="line1"), out, project_id="proj")
    assert report.job_ids == ["001"]
    req = remote.canvas_requests[0]
    assert req["model"] == "aud9"  # 名称换成了 ID
    assert req["mode"] == "audio" and req["ref_audios"] == ["voiceA"]  # 参考音色直接传媒资 ID
    assert req["short_series_project_id"] == "proj" and "project_id" not in req  # 不传画布 ID
    assert log(out)[0]["state"] == "submitted"

    remote.finish_canvas("c1", "a1", b"RIFF", kind="audio")
    fetched = await gen.fetch(remote, out, job_ids=report.job_ids, http=remote.http())
    assert (out / "line1.wav").read_bytes() == b"RIFF"
    assert fetched.rows == [("001", "done", "line1.wav")] and not fetched.failed
    assert log(out)[0]["state"] == "done"
    assert "dl.example.com" not in (out / gen.LOG).read_text()  # 下载地址不落进记录


async def test_same_inputs_skip_and_again_makes_new_batch(tmp_path, remote):
    out = tmp_path
    await gen.gen(remote, "audio", audio_params(), out)
    remote.finish_canvas("c1", "a1", kind="audio")
    await gen.fetch(remote, out, http=remote.http())
    first = log(out)[0]["tasks"][0]["file"]
    assert first.startswith("audio-") and first.endswith(".wav")

    skipped = await gen.gen(remote, "audio", audio_params(), out)
    assert "skipped" in skipped.summary and len(remote.canvas_requests) == 1

    again = await gen.gen(remote, "audio", audio_params(), out, again=True)
    remote.finish_canvas("c2", "a2", kind="audio")
    await gen.fetch(remote, out, job_ids=again.job_ids, http=remote.http())
    second = log(out)[1]["tasks"][0]["file"]
    assert second == first.replace(".wav", "-b2.wav")  # 同参数的第二批不覆盖第一批


async def test_submit_timeout_blocks_resubmit(tmp_path, remote):
    remote.submit_outcome = "timeout"  # 服务端已受理，响应丢了
    with pytest.raises(NetworkError):
        await gen.gen(remote, "audio", audio_params(), tmp_path)
    assert log(tmp_path)[0]["state"] == "submitting"

    remote.submit_outcome = "ok"
    with pytest.raises(PreflightError) as e:
        await gen.gen(remote, "audio", audio_params(), tmp_path)
    assert e.value.code == "pending_submit"
    assert len(remote.canvas_requests) == 1  # 没有重复提交


async def test_server_rejection_marks_failed_and_allows_retry(tmp_path, remote, monkeypatch):
    async def reject(*args, **kwargs):
        raise ToolError("40001", tool="canvas_generate_audio")

    monkeypatch.setattr(gen.canvas, "submit", reject)
    with pytest.raises(ToolError):
        await gen.gen(remote, "audio", audio_params(), tmp_path)
    assert log(tmp_path)[0]["state"] == "failed"
    monkeypatch.undo()
    report = await gen.gen(remote, "audio", audio_params(), tmp_path)
    assert report.job_ids == ["002"]


@pytest.mark.parametrize(
    ("kind", "params", "code"),
    [
        ("video", {"prompt": "x", "model": "vid9", "duration": 5}, "bad_duration"),
        ("video", {"prompt": "x", "model": "vid9", "mode": "firstlast"}, "missing_frame"),
        ("image", {"prompt": "x", "model": "nope"}, "unknown_model"),
        ("image", {"prompt": "x", "model": "img9", "aspect": "21:9"}, "bad_aspect"),
        ("image", {"prompt": "x", "model": "img9", "count": 5}, "bad_count"),
        ("audio", {"prompt": "x", "model": "MiniMax-2.8-HD", "voices": ["v"]}, "no_ref_audio"),
        ("audio", {"prompt": "  ", "model": "aud9"}, "missing_prompt"),
    ],
)
async def test_preflight_rejects_before_submitting(tmp_path, remote, kind, params, code):
    with pytest.raises(PreflightError) as e:
        await gen.gen(remote, kind, params, tmp_path)
    assert e.value.code == code
    assert not remote.canvas_requests and not (tmp_path / gen.LOG).exists()


async def test_dry_run_writes_nothing(tmp_path, remote):
    report = await gen.gen(remote, "audio", audio_params(), tmp_path, dry_run=True)
    assert "would submit" in report.summary
    assert not remote.canvas_requests and not (tmp_path / gen.LOG).exists()


async def test_video_firstlast_uses_plain_frame_urls(tmp_path, remote):
    remote.add_asset("k1", "image", b"a", "k1.png")
    remote.add_asset("k2", "image", b"b", "k2.png")
    params = {"prompt": "推镜", "model": "seedance-2.0 mini", "mode": "firstlast",
              "image": "k1", "last": "k2", "duration": 8, "resolution": 1}  # fmt: skip
    await gen.gen(remote, "video", params, tmp_path, http=remote.http())
    req = remote.canvas_requests[0]
    assert req["mode"] == "FirstLastFrame" and req["model"] == "vid9"
    assert (
        req["first_frame_url"] == "https://dl.example.com/k1"
        and req["last_frame_url"] == "https://dl.example.com/k2"
    )
    assert req["enable_sound"] is False  # 远端默认开音效，这里显式关
    assert req["duration"] == 8 and req["resolution"] == 1


async def test_image_refs_must_exist(tmp_path, remote):
    params = {"prompt": "第一张图片", "model": "img9", "refs": ["ghost"]}
    with pytest.raises(PreflightError) as e:
        await gen.gen(remote, "image", params, tmp_path, http=remote.http())
    assert e.value.code == "ref_asset_not_found"


async def test_partial_failure_and_resume_after_timeout(tmp_path, remote):
    params = {"prompt": "x", "model": "img9", "count": 2, "name": "pic"}
    report = await gen.gen(remote, "image", params, tmp_path)
    remote.finish_canvas("c1", "i1", b"one")
    waited = await gen.fetch(remote, tmp_path, job_ids=report.job_ids, timeout=0, http=remote.http())
    assert waited.timed_out == 1 and (tmp_path / "pic-1.png").exists()
    assert log(tmp_path)[0]["state"] == "submitted"  # 还有一张在跑

    remote.finish_canvas("c2", None)  # 第二张失败
    resumed = await gen.fetch(remote, tmp_path, http=remote.http())
    assert resumed.failed == 1 and not resumed.timed_out
    assert remote.http_hits.count("i1") == 1  # 已下载的不重复下载
    assert log(tmp_path)[0]["state"] == "done"
    assert await gen.fetch(remote, tmp_path, http=remote.http()) is not None  # 没有待处理的记录也不出错


async def test_music_lyrics_modes(tmp_path, remote):
    await gen.gen(remote, "music", {"prompt": "钢琴", "model": "mus1"}, tmp_path)
    await gen.gen(remote, "music", {"prompt": "钢琴", "model": "mus1", "lyrics": "ai"}, tmp_path)
    await gen.gen(remote, "music", {"prompt": "钢琴", "model": "mus1", "lyrics": "第一句"}, tmp_path)
    modes = [(r["mode"], r["lyrics_mode"], r.get("lyrics")) for r in remote.canvas_requests]
    assert modes == [("music", "none", None), ("music", "ai", None), ("music", "input", "第一句")]


async def test_new_params_under_same_name_do_not_overwrite(tmp_path, remote):
    await gen.gen(remote, "audio", audio_params(name="line"), tmp_path)
    remote.finish_canvas("c1", "a1", b"old", kind="audio")
    await gen.fetch(remote, tmp_path, http=remote.http())
    await gen.gen(remote, "audio", audio_params(name="line", prompt="他说：「慢一点」"), tmp_path)
    remote.finish_canvas("c2", "a2", b"new", kind="audio")
    await gen.fetch(remote, tmp_path, http=remote.http())
    assert (tmp_path / "line.wav").read_bytes() == b"old"
    assert (tmp_path / "line-b2.wav").read_bytes() == b"new"
