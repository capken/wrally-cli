"""wait：等待任务完成。先查一次，有任务在跑才按经验时长等待，再低频轮询；只在结束时输出一次。"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio

from wrally.api.image import canvas_task
from wrally.api.results import get_results
from wrally.commands.common import error_cell, flow_seq, for_each, load_manifest, section, select
from wrally.commands.fetch import KINDS, STATUS
from wrally.errors import WrallyError
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

INITIAL = {"keyframe": 40, "video": 120, "audio": 40}  # 首次轮询前的经验等待时长（秒）
INTERVAL = {"keyframe": 10, "video": 30, "audio": 10}
HEADERS = ("shot", "status", "file")


@dataclass
class Progress:
    state: str  # running / done / failed / none
    done: int = 0
    failed: int = 0
    canvas: bool = False
    assets: list[str] = field(default_factory=list)


@dataclass
class WaitReport:
    kind: str
    rows: list[tuple[str, ...]] = field(default_factory=list)
    elapsed: float = 0
    failed: int = 0
    timed_out: int = 0

    @property
    def summary(self) -> str:
        done = sum(r[1] == "done" for r in self.rows)
        tail = f", {self.timed_out} still running (timeout)" if self.timed_out else ""
        return f"waited {self.elapsed:.0f}s for {self.kind}: {done} done, {self.failed} failed{tail}"


def _canvas_tasks(kind: str, entry: dict) -> list[str]:
    return list((entry.get("keyframe") or {}).get("canvas_tasks") or []) if kind == "keyframe" else []


async def wait(
    ws: Workspace,
    client: McpClient,
    kind: str,
    spec: str = "all",
    *,
    timeout: float = 1200,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> WaitReport:
    data = load_manifest(ws)
    entries = select(data, spec)
    started = clock()
    state: dict[str, Progress | WrallyError] = {}
    baseline: dict[str, int] = {}

    async def progress(entry: dict) -> Progress:
        key = entry["key"]
        tasks = _canvas_tasks(kind, entry)
        if tasks:  # 带参考图的镜头走画布，进度要按任务 ID 查
            results = [
                await canvas_task(client, t, data["project_id"], ws.raw_dir / f"{key}-wait-canvas-{t}.json")
                for t in tasks
            ]
            assets = [a for r in results for a in r.asset_ids]
            if any(r.state == "running" for r in results):
                return Progress("running", canvas=True)
            return Progress("done" if assets else "failed", canvas=True, assets=assets)
        rs = await get_results(client, entry["story_id"], KINDS[kind], ws.raw_dir / f"{key}-wait-{kind}.json")  # type: ignore[arg-type]
        return Progress(rs.state, rs.done, rs.failed)

    async def poll(targets: list[dict]) -> None:
        for entry, p in zip(targets, await for_each(targets, progress), strict=True):
            state[str(entry["key"])] = p

    def pending(entry: dict) -> bool:
        """这一镜是否还要继续等。刚提交的任务可能还没出现在 doing 里：
        清单标着 running、结果数和失败数都没超过提交时的基线，就还没出结果。"""
        p = state[str(entry["key"])]
        if not isinstance(p, Progress):
            return False
        if p.state == "running":
            return True
        sec = entry.get(kind) or {}
        if p.canvas or sec.get("status") != "running" or "results_before" not in sec:
            return False
        return p.done <= int(sec["results_before"]) and p.failed <= int(sec.get("failed_before") or 0)

    def running() -> list[dict]:
        return [e for e in entries if pending(e)]

    await poll(entries)
    for entry in entries:
        key, p, sec = str(entry["key"]), state[str(entry["key"])], entry.get(kind) or {}
        if "failed_before" in sec and sec.get("status") == "running":
            baseline[key] = int(sec["failed_before"])  # 提交时记下的基线最准
        elif isinstance(p, Progress) and p.state == "running":
            baseline[key] = p.failed  # 之后失败数增加，说明这次的任务失败了
    if running():
        await sleep(INITIAL[kind])
        while True:
            await poll(running())
            if not running() or clock() - started >= timeout:
                break
            await sleep(INTERVAL[kind])

    report = WaitReport(kind, elapsed=clock() - started)
    for entry in entries:
        key = str(entry["key"])
        p = state[key]
        file = str((entry.get(kind) or {}).get("file") or "")
        if isinstance(p, WrallyError):
            report.failed += 1
            report.rows.append((key, error_cell(p), file))
            continue
        label = STATUS[p.state]
        if pending(entry):
            label = "running"
            report.timed_out += 1
        elif not p.canvas and key in baseline and p.failed > baseline[key]:
            label = "failed"
        report.failed += label == "failed"
        if kind != "keyframe" or "status" in (entry.get(kind) or {}):
            section(entry, kind)["status"] = label
        if p.canvas and label != "running":
            kf = section(entry, "keyframe")
            kf.pop("canvas_tasks", None)
            if p.assets:
                kf["canvas_assets"] = flow_seq(
                    list(dict.fromkeys([*(kf.get("canvas_assets") or []), *p.assets]))
                )
        report.rows.append((key, label, file))
    ws.save(data)
    report.rows.sort(key=lambda r: r[1] == "done")
    return report
