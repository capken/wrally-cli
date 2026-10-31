"""fetch：下载最新结果到 .wrally/。

get_result 只取 asset_id → asset_detail 一次批量取地址 → 并发下载。
已下载且 asset_id 没变的文件不重复下载；重跑从断点继续。
"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from wrally.api.assets import download_assets
from wrally.api.results import ResultSet, get_results
from wrally.commands.common import error_cell, flow_seq, for_each, letter, load_manifest, section, select
from wrally.errors import WrallyError
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

KINDS = {"keyframe": "image", "video": "video", "audio": "audio"}
STATUS = {"running": "running", "done": "done", "failed": "failed", "none": "pending"}


@dataclass
class FetchReport:
    kind: str
    headers: tuple[str, ...]
    rows: list[tuple[str, ...]] = field(default_factory=list)
    downloaded: int = 0
    up_to_date: int = 0
    failed: int = 0

    @property
    def summary(self) -> str:
        return (f"fetched {self.kind}: {self.downloaded} downloaded, "
                f"{self.up_to_date} up to date, {self.failed} failed")  # fmt: skip


def _rel(ws: Workspace, path: Path) -> str:
    return path.relative_to(ws.root).as_posix()


async def fetch(ws: Workspace, client: McpClient, kind: str, spec: str = "all",
                http: httpx2.AsyncClient | None = None) -> FetchReport:  # fmt: skip
    data = load_manifest(ws)
    entries = select(data, spec)

    async def query(entry: dict) -> ResultSet:
        raw = ws.raw_dir / f"{entry['key']}-fetch-{kind}.json"
        return await get_results(client, entry["story_id"], KINDS[kind], raw)  # type: ignore[arg-type]

    results = await for_each(entries, query)
    raw_assets = ws.raw_dir / f"fetch-{kind}-assets.json"
    if kind == "keyframe":
        report = await _keyframes(ws, client, entries, results, raw_assets, http)
    else:
        report = await _media(ws, client, kind, entries, results, raw_assets, http)
    ws.save(data)
    report.rows.sort(key=lambda r: not r[1].startswith(("error", "failed")))
    return report


async def _media(ws, client, kind, entries, results, raw_assets, http) -> FetchReport:
    report = FetchReport(kind, ("shot", "status", "file"))
    plan: list[tuple[str, Path]] = []
    owners: list[dict] = []
    for entry, rs in zip(entries, results, strict=True):
        key = str(entry["key"])
        if isinstance(rs, WrallyError):
            report.failed += 1
            report.rows.append((key, error_cell(rs), ""))
            continue
        sec = entry.get(kind) or {}
        if rs.state != "done":
            section(entry, kind)["status"] = STATUS[rs.state]
            report.failed += rs.state == "failed"
            report.rows.append((key, STATUS[rs.state], str(sec.get("file") or "")))
            continue
        latest = rs.latest
        current = sec.get("file")
        if sec.get("asset_id") == latest.asset_id and current and (ws.root / current).exists():
            if sec.get("status") == "failed":  # 这次的任务失败了，最新结果还是上一条，不能记成完成
                report.failed += 1
                report.rows.append((key, "failed", str(current)))
                continue
            section(entry, kind)["status"] = "done"
            report.up_to_date += 1
            report.rows.append((key, "done", str(current)))
            continue
        plan.append((latest.asset_id, ws.state_dir / kind / key))
        owners.append(entry)

    for entry, dl in zip(
        owners, await download_assets(client, plan, raw_path=raw_assets, http=http), strict=True
    ):
        key = str(entry["key"])
        if not dl.ok:
            report.failed += 1
            report.rows.append((key, f"error: {dl.error}", ""))
            continue
        sec = section(entry, kind)
        if sec.get("asset_id") != dl.asset_id:
            for measured in ("f0", "channel_gap", "noise_floor"):  # 换了一条旁白，旧的量值作废
                sec.pop(measured, None)
        sec["asset_id"] = dl.asset_id
        if dl.asset and dl.asset.task_id:
            sec["task"] = dl.asset.task_id
        sec["status"] = "done"
        sec["file"] = _rel(ws, dl.path)
        report.downloaded += 1
        report.rows.append((key, "done", sec["file"]))
    order = [str(e["key"]) for e in entries]
    report.rows.sort(key=lambda r: order.index(r[0]))
    return report


def _has(directory: Path, stem: str) -> bool:
    return any(directory.glob(f"{stem}.*"))


async def _keyframes(ws, client, entries, results, raw_assets, http) -> FetchReport:
    """候选按生成时间从旧到新编号 A、B、C…，新候选只会追加，已有字母不变。"""
    report = FetchReport("keyframe", ("shot", "status", "candidates", "pick"))
    plan: list[tuple[str, Path]] = []
    owners: list[str] = []
    info: dict[str, dict] = {}
    for entry, rs in zip(entries, results, strict=True):
        key = str(entry["key"])
        if isinstance(rs, WrallyError):
            report.failed += 1
            report.rows.append((key, error_cell(rs), "", ""))
            continue
        kf = entry.get("keyframe") or {}
        old = list(kf.get("candidates") or [])
        # 已有候选保持原位，新的只往后追加，字母才不会变；画布生成的图排在场次结果之后
        seen = [r.asset_id for r in rs.results] + list(kf.get("canvas_assets") or [])
        ids = old + [a for a in dict.fromkeys(seen) if a not in old]
        directory = ws.state_dir / "keyframes" / key
        new = 0
        for i, asset_id in enumerate(ids):
            if i < len(old) and _has(directory, letter(i)):
                report.up_to_date += 1
                continue
            plan.append((asset_id, directory / letter(i)))
            owners.append(key)
            new += 1
        picked = kf.get("asset_id")
        # 选用的图可能不在候选里（网页上传、画布生成），单独下载一份供核对和检查用
        if picked and picked not in ids and not _has(directory, f"pick-{picked}"):
            plan.append((picked, directory / f"pick-{picked}"))
            owners.append(key)
        if ids:
            section(entry, "keyframe")["candidates"] = flow_seq(ids)
        pick = letter(ids.index(picked)) if picked in ids else ("file" if picked else "-")
        info[key] = {"state": STATUS[rs.state], "count": len(ids), "new": new, "pick": pick, "errors": []}

    for key, dl in zip(
        owners, await download_assets(client, plan, raw_path=raw_assets, http=http), strict=True
    ):
        if dl.ok:
            report.downloaded += 1
        else:
            report.failed += 1
            info[key]["errors"].append(dl.error)
    for key, i in info.items():
        state = f"error: {i['errors'][0]}" if i["errors"] else i["state"]
        report.rows.append(
            (key, state, f"{i['count']} ({i['new']} new)" if i["new"] else str(i["count"]), i["pick"])
        )
    order = [str(e["key"]) for e in entries]
    report.rows.sort(key=lambda r: order.index(r[0]))
    return report
