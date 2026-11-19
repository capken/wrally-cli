"""三类场次生成命令共用骨架（commands/shot_gen.py）的单元测试。"""

import pytest
from conftest import entry

from wrally.api.results import ResultSet
from wrally.commands.shot_gen import (
    GenReport,
    classify,
    inspect_and_recover,
    mark_submitting,
    record_batch_submit,
    skip_marked,
)
from wrally.errors import PreflightError


def test_classify_handles_all_idempotency_states_and_dry_run(ws):
    data = ws.load()
    entries = list(data["shots"])
    # 00: running 且输入变了 → 跳过（等待旧任务）
    entries[0]["video"] = {"status": "running", "made_from": "old"}
    # 01: submitting 且输入未变 → 进入 recover
    entries[1]["video"] = {"status": "submitting", "made_from": "fp01", "results_before": 0}
    # 02: done 且无 made_from → 外部产物，跳过
    entries[2]["video"] = {"status": "done", "asset_id": "ext"}
    # 99: 尚无记录 → 进入 todo
    prints = {"00": "fp00", "01": "fp01", "02": "fp02", "99": "fp99"}
    report = GenReport(noun="video")
    todo, recover = classify(entries, prints, "video", report, dry_run=True)
    assert [e["key"] for e in todo] == ["99"]
    assert [e["key"] for e in recover] == ["01"]
    assert report.dry_run is True
    assert report.rows == [
        ("00", "skipped", "another task is running; wait for it first"),
        ("02", "skipped", "done, made outside wrally (--redo to regenerate)"),
        ("99", "would submit", ""),
        ("01", "would check", "interrupted run; remote decides"),
    ]


def test_skip_marked_filters_only_matching_kind(ws):
    data = ws.load()
    data["shots"][0]["skip"] = ["video"]
    data["shots"][1]["skip"] = ["audio"]
    report = GenReport(noun="video")
    left = skip_marked(list(data["shots"]), "video", report)
    assert [e["key"] for e in left] == ["01", "02", "99"]
    assert report.rows == [("00", "skipped", "marked skip in episode.yaml")]


async def test_inspect_and_recover_restores_manifest_order_and_marks_done(ws, remote):
    data = ws.load()
    entries = list(data["shots"])
    # 00 是中断后远端没受理（应回到 todo 并排在 02 前面）；01 是中断后远端已完成（应标成 done）
    entries[0]["video"] = {"status": "submitting", "made_from": "fp00", "results_before": 0}
    entries[1]["video"] = {"status": "submitting", "made_from": "fp01", "results_before": 0}
    ws.save(data)
    remote.add_result("s01", "video", "v1", "2026-10-06 12:00:00")

    report = GenReport(noun="video")
    state = await inspect_and_recover(
        ws,
        remote,
        data,
        entries,
        todo=[entries[2]],
        recover=[entries[0], entries[1]],
        kind="video",
        report=report,
    )
    assert [e["key"] for e in state.todo] == ["00", "02"]
    assert entries[1]["video"]["status"] == "done"
    assert report.rows == [("01", "skipped", "already submitted by an interrupted run")]


async def test_inspect_and_recover_raises_when_shot_missing_on_remote(ws, remote):
    data = ws.load()
    data["shots"][0]["story_id"] = "gone"
    report = GenReport(noun="audio")
    with pytest.raises(PreflightError) as err:
        await inspect_and_recover(
            ws,
            remote,
            data,
            list(data["shots"]),
            todo=[data["shots"][0]],
            recover=[],
            kind="audio",
            report=report,
        )
    assert err.value.code == "missing_on_remote" and "(00)" in err.value.hint


def test_mark_submitting_and_record_batch_submit_single_and_multi_tasks(ws):
    data = ws.load()
    shots = [data["shots"][0], data["shots"][1]]
    counts = {
        "00": ResultSet(kind="audio", done=2, failed=1),
        "01": ResultSet(kind="audio", done=0, failed=0),
    }

    # 单任务模式（video / audio）
    mark_submitting(ws, data, shots, "audio", {"00": "fa", "01": "fb"}, counts)
    assert entry(ws, "00")["audio"]["status"] == "submitting"
    assert entry(ws, "00")["audio"]["results_before"] == 2
    assert entry(ws, "00")["audio"]["failed_before"] == 1

    report = GenReport(noun="audio")
    raw = ws.raw_dir / "audio-submit.json"
    record_batch_submit(
        ws,
        data,
        shots,
        "audio",
        {"task_ids": ["t-01"], "skipped": ["s00"]},
        raw,
        report,
        stamp="2026-11-19T10:00:00",
    )
    assert entry(ws, "00")["audio"]["status"] == "failed"
    assert entry(ws, "01")["audio"]["status"] == "running"
    assert entry(ws, "01")["audio"]["task"] == "t-01"
    assert report.failed == 1 and report.submitted == ["01"]

    # 多任务模式（keyframe）
    mark_submitting(ws, data, shots, "keyframe", {"00": "fk0", "01": "fk1"}, counts, task_key="tasks")
    kf_report = GenReport(noun="keyframe")
    record_batch_submit(
        ws,
        data,
        shots,
        "keyframe",
        {"task_ids": ["k0a", "k0b", "k1a", "k1b"], "skipped": []},
        ws.raw_dir / "keyframe-submit.json",
        kf_report,
        stamp="2026-11-19T10:00:00",
        task_key="tasks",
        detail=lambda _e, _t: "2 images",
    )
    assert list(entry(ws, "00")["keyframe"]["tasks"]) == ["k0a", "k0b"]
    assert list(entry(ws, "01")["keyframe"]["tasks"]) == ["k1a", "k1b"]
    assert kf_report.rows == [("00", "submitted", "2 images"), ("01", "submitted", "2 images")]
