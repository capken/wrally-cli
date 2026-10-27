import pytest
from conftest import SIGNED, entry

from wrally.api import assets as assets_api
from wrally.api.assets import asset_details, download_assets
from wrally.api.results import ResultSet, get_results
from wrally.commands.fetch import fetch


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(assets_api, "RETRY_DELAYS", (0, 0, 0))


def test_state_ignores_old_failures_when_results_exist():
    assert ResultSet(kind="video", doing=1, failed=0).state == "running"
    assert ResultSet(kind="video", failed=2, results=[{"asset_id": "a"}]).state == "done"
    assert ResultSet(kind="video", failed=1).state == "failed"
    assert ResultSet(kind="video").state == "none"


async def test_get_results_drops_urls_and_sorts_oldest_first(remote):
    remote.add_result("s02", "video", "v2", "2026-10-05 19:00:00")
    remote.add_result("s02", "video", "v1", "2026-10-05 18:00:00")
    rs = await get_results(remote, "s02", "video")
    assert [r.asset_id for r in rs.results] == ["v1", "v2"] and rs.latest.asset_id == "v2"
    assert "http" not in rs.model_dump_json()


async def test_asset_details_batches_by_50_and_hides_url(remote):
    for i in range(120):
        remote.add_asset(f"a{i}", "image", b"", "x.png")
    found = await asset_details(remote, [f"a{i}" for i in range(120)] + ["ghost"])
    assert len(found) == 120 and "ghost" not in found
    assert [len(a["asset_ids"]) for t, a in remote.calls if t == "asset_detail"] == [50, 50, 21]
    assert "http" not in found["a0"].model_dump_json() and "http" not in repr(found["a0"])


async def test_download_adds_suffix_and_refetches_address_on_403(remote, tmp_path):
    """签名地址约 10 小时过期：403 时重新取地址后重试 1 次。"""
    remote.add_asset("v1", "video", b"movie", "05.mp4")
    remote.expired.add("v1")
    [dl] = await download_assets(remote, [("v1", tmp_path / "video" / "05")], http=remote.http())
    assert dl.ok and dl.path == tmp_path / "video" / "05.mp4" and dl.path.read_bytes() == b"movie"
    assert remote.count("asset_detail") == 2 and remote.http_hits == ["v1", "v1"]
    assert not list(tmp_path.rglob("*.part"))


async def test_download_failures_are_per_item(remote, tmp_path):
    remote.add_asset("ok", "image", b"img", "a.png")
    got = await download_assets(
        remote, [("ghost", tmp_path / "g"), ("ok", tmp_path / "o")], http=remote.http()
    )
    assert [(d.asset_id, d.error) for d in got] == [("ghost", "asset_not_found"), ("ok", "")]


async def test_fetch_video_records_state_and_is_idempotent(ws, remote):
    remote.add_result("s02", "video", "v1", "2026-10-05 18:00:00", b"old")
    remote.add_result("s02", "video", "v2", "2026-10-05 19:00:00", b"new")
    remote.add_result("s01", "video", "w1", "2026-10-05 18:00:00", b"w")
    report = await fetch(ws, remote, "video", "01,02", http=remote.http())
    video = entry(ws, "02")["video"]
    assert dict(video) == {
        "asset_id": "v2",
        "task": "task-v2",
        "status": "done",
        "file": ".wrally/video/02.mp4",
    }
    assert (ws.root / video["file"]).read_bytes() == b"new"
    assert (report.downloaded, report.up_to_date, report.failed) == (2, 0, 0)
    assert remote.count("asset_detail") == 1  # 一次批量取地址

    again = await fetch(ws, remote, "video", "01,02", http=remote.http())
    assert (again.downloaded, again.up_to_date) == (0, 2)
    assert sorted(remote.http_hits) == ["v2", "w1"]  # 没有重复下载
    assert SIGNED not in ws.manifest_path.read_text(encoding="utf-8")


async def test_newer_result_replaces_file_and_drops_stale_f0(ws, remote):
    remote.add_result("s02", "audio", "a1", "2026-10-05 18:00:00", b"one")
    await fetch(ws, remote, "audio", "02", http=remote.http())
    data = ws.load()
    next(e for e in data["shots"] if e["key"] == "02")["audio"].update(f0=86, channel_gap=42.3)
    ws.save(data)
    remote.add_result("s02", "audio", "a2", "2026-10-05 19:00:00", b"two")
    await fetch(ws, remote, "audio", "02", http=remote.http())
    audio = entry(ws, "02")["audio"]
    assert audio["asset_id"] == "a2" and "f0" not in audio and "channel_gap" not in audio
    assert (ws.root / audio["file"]).read_bytes() == b"two"


async def test_running_and_failed_shots_are_not_downloaded(ws, remote):
    remote.add_result("s01", "video", "w1", "2026-10-05 18:00:00")
    remote.results[("s01", "video")]["doing"] = 1
    remote.results[("s02", "video")] = {"doing": 0, "failed": 1, "results": []}
    report = await fetch(ws, remote, "video", "all", http=remote.http())
    assert {r[0]: r[1] for r in report.rows} == {
        "00": "pending",
        "01": "running",
        "02": "failed",
        "99": "pending",
    }
    assert remote.http_hits == [] and report.failed == 1
    assert entry(ws, "01")["video"]["status"] == "running"


async def test_one_broken_shot_does_not_stop_the_others(ws, remote):
    remote.add_result("s02", "video", "v1", "2026-10-05 18:00:00")
    remote.broken_stories.add("s01")
    report = await fetch(ws, remote, "video", "all", http=remote.http())
    rows = {r[0]: r[1] for r in report.rows}
    assert rows["01"].startswith("error: 50001  (raw: ") and rows["01"].endswith("01-fetch-video.json)")
    assert rows["02"] == "done" and report.failed == 1
    assert report.rows[0][0] == "01"  # 出错的行排最前


async def test_keyframe_candidates_are_lettered_and_only_appended(ws, remote):
    remote.add_result("s01", "image", "i1", "2026-10-05 14:00:00", b"A")
    remote.add_result("s01", "image", "i2", "2026-10-05 15:00:00", b"B")
    await fetch(ws, remote, "keyframe", "01", http=remote.http())
    folder = ws.state_dir / "keyframes" / "01"
    assert (folder / "A.png").read_bytes() == b"A" and (folder / "B.png").read_bytes() == b"B"
    assert list(entry(ws, "01")["keyframe"]["candidates"]) == ["i1", "i2"]
    assert "candidates: [i1, i2]" in ws.manifest_path.read_text(encoding="utf-8")

    remote.add_result("s01", "image", "i3", "2026-10-05 16:00:00", b"C")
    report = await fetch(ws, remote, "keyframe", "01", http=remote.http())
    assert remote.http_hits == ["i1", "i2", "i3"] or sorted(remote.http_hits) == ["i1", "i2", "i3"]
    assert (report.downloaded, report.up_to_date) == (1, 2)
    assert report.rows[0] == ("01", "done", "3 (1 new)", "-")


async def test_pick_outside_candidates_is_downloaded_separately(ws, remote):
    """网页上传或画布生成的选用图不在场次候选里。"""
    remote.add_result("s02", "image", "i1", "2026-10-05 14:00:00")
    remote.add_asset("key02", "image", b"picked", "02.png")
    report = await fetch(ws, remote, "keyframe", "02", http=remote.http())
    assert (ws.state_dir / "keyframes" / "02" / "pick-key02.png").read_bytes() == b"picked"
    assert report.rows[0][3] == "file"
    assert entry(ws, "02")["keyframe"]["asset_id"] == "key02"  # 选用不被 fetch 改动
