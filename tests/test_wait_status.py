from conftest import entry

from wrally.commands.status import status
from wrally.commands.wait import wait


class Clock:
    def __init__(self, remote, finish_after: float, story=("s02", "video")) -> None:
        self.now, self.sleeps, self.remote, self.finish_after, self.story = (
            0.0,
            [],
            remote,
            finish_after,
            story,
        )

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.now >= self.finish_after:
            self.remote.results[self.story]["doing"] = 0


async def test_wait_returns_immediately_when_nothing_is_running(ws, remote):
    remote.add_result("s02", "video", "v1", "2026-10-05 18:00:00")
    clock = Clock(remote, 0)
    report = await wait(ws, remote, "video", "02", sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [] and report.rows == [("02", "done", "")]
    assert remote.count("shotlist_get_result") == 1


async def test_wait_sleeps_experience_time_then_polls_slowly(ws, remote):
    """视频：首次轮询前等 120 秒，之后每 30 秒一次；只轮询还在跑的镜头。"""
    remote.add_result("s01", "video", "w1", "2026-10-05 18:00:00")
    remote.add_result("s02", "video", "v1", "2026-10-05 18:00:00")
    remote.results[("s02", "video")]["doing"] = 1
    clock = Clock(remote, 170)
    report = await wait(ws, remote, "video", "01,02", sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [120, 30, 30]
    assert remote.count("shotlist_get_result") == 2 + 3  # 第一轮两镜，之后只查 02
    assert report.summary == "waited 180s for video: 2 done, 0 failed"
    assert entry(ws, "02")["video"]["status"] == "done"


async def test_image_and_audio_poll_every_10s(ws, remote):
    remote.results[("s02", "audio")] = {"doing": 1, "failed": 0, "results": []}
    clock = Clock(remote, 55, ("s02", "audio"))
    await wait(ws, remote, "audio", "02", sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [40, 10, 10]


async def test_wait_times_out_and_reports_still_running(ws, remote):
    remote.results[("s02", "video")] = {"doing": 1, "failed": 0, "results": []}
    clock = Clock(remote, 10**9)
    report = await wait(ws, remote, "video", "02", timeout=200, sleep=clock.sleep, clock=clock)
    assert report.timed_out == 1 and report.rows == [("02", "running", "")]
    assert entry(ws, "02")["video"]["status"] == "running"


async def test_wait_never_sleeps_past_timeout(ws, remote):
    """--timeout 比首次等待（视频 120 秒）还短时，按 --timeout 截断，不多睡。"""
    remote.results[("s02", "video")] = {"doing": 1, "failed": 0, "results": []}
    clock = Clock(remote, 10**9)
    report = await wait(ws, remote, "video", "02", timeout=50, sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [50] and report.timed_out == 1


async def test_new_failure_is_reported_even_when_old_results_exist(ws, remote):
    remote.add_result("s02", "video", "v1", "2026-10-05 18:00:00")
    remote.results[("s02", "video")]["doing"] = 1

    class Failing(Clock):
        async def sleep(self, seconds: float) -> None:
            await super().sleep(seconds)
            self.remote.results[self.story]["failed"] = 1

    clock = Failing(remote, 100)
    report = await wait(ws, remote, "video", "02", sleep=clock.sleep, clock=clock)
    assert report.rows == [("02", "failed", "")] and report.failed == 1


async def test_later_wait_keeps_failed_until_a_new_result_appears(ws, remote):
    """任务失败后再 wait：远端因旧结果显示完成，本地仍记失败；出了新结果才算完成。"""
    remote.add_result("s02", "audio", "a1", "2026-10-05 18:00:00")
    remote.results[("s02", "audio")]["failed"] = 1
    data = ws.load()
    shot = next(e for e in data["shots"] if e["key"] == "02")
    shot["audio"] = {"status": "failed", "results_before": 1, "failed_before": 0}
    ws.save(data)
    clock = Clock(remote, 0, ("s02", "audio"))
    report = await wait(ws, remote, "audio", "02", sleep=clock.sleep, clock=clock)
    assert report.rows == [("02", "failed", "")] and entry(ws, "02")["audio"]["status"] == "failed"

    remote.add_result("s02", "audio", "a2", "2026-10-05 19:00:00")
    report = await wait(ws, remote, "audio", "02", sleep=clock.sleep, clock=clock)
    assert entry(ws, "02")["audio"]["status"] == "done"


def test_status_reads_only_the_manifest(ws):
    data = ws.load()
    shots = {e["key"]: e for e in data["shots"]}
    shots["01"]["keyframe"] = {"candidates": ["i1", "i2"], "asset_id": "i2"}
    shots["01"]["video"] = {"status": "done", "file": ".wrally/video/01.mp4"}
    shots["01"]["audio"] = {"status": "done", "file": ".wrally/audio/01.wav", "f0": 86}
    shots["99"]["video"] = {"status": "done", "file": ".wrally/video/99.mp4"}
    ws.save(data)
    for rel in (".wrally/video/01.mp4", ".wrally/audio/01.wav"):
        (ws.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws.root / rel).write_bytes(b"x")
    line, rows = status(ws)
    assert line == "4 shots: keyframe 2 picked, video 1 done, audio 1 done"
    by_key = {r[0]: r[1:] for r in rows}
    assert by_key["01"] == ("B/2", "done", "done", "86")
    assert by_key["02"] == ("set/0", "-", "-", "-")
    assert by_key["99"][1] == "done (no file)"
    assert rows[-1][0] == "01"  # 做完的排最后


def test_status_in_a_project_directory_lists_episodes(tmp_path, stories):
    """项目目录里没有清单：status 每集一行，其余命令提示要到某一集的目录下跑。"""
    from typer.testing import CliRunner

    from wrally.api.models import Shot
    from wrally.cli import app
    from wrally.commands.pull import merge
    from wrally.commands.status import project_status
    from wrally.workspace import Workspace

    (tmp_path / "wrally.yaml").write_text("project_id: proj\n", encoding="utf-8")
    for name in ("ep2", "ep1"):
        ep = Workspace(tmp_path / name)
        ep.root.mkdir()
        merge(ep, [Shot.from_story(s) for s in stories], project_id="proj", series_id=name)
    (tmp_path / "notes").mkdir()  # 没有清单的子目录不算一集
    data = ep.load()
    data["shots"][1]["video"] = {"status": "done", "file": ".wrally/video/01.mp4"}
    ep.save(data)
    for rel in (".wrally/video/01.mp4", "ep1.mp4"):
        (ep.root / rel).parent.mkdir(parents=True, exist_ok=True)
        (ep.root / rel).write_bytes(b"x")
    line, rows = project_status(tmp_path)
    assert line.startswith("2 episodes: 1 with a film")
    assert rows == [("ep1", "4", "1", "1", "0", "yes"), ("ep2", "4", "1", "0", "0", "-")]
    result = CliRunner().invoke(app, ["status", "-C", str(tmp_path)])
    assert result.exit_code == 0 and result.output.splitlines()[0] == line
    result = CliRunner().invoke(app, ["lint", "-C", str(tmp_path)])
    assert result.exit_code == 2 and "project directory: use -C <episode>" in result.output
