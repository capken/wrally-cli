"""verify-picks：核对清单选图与网页「选用」是否一致。

网页上的「选用」就是场次的 gen_config.video.singleAssets。先比 asset_id；
ID 不同再下载两张图逐像素比对（网页手工上传同一张图会产生新的 asset_id）。
"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from wrally.api.assets import download_assets
from wrally.api.shots import shot_list
from wrally.commands.common import load_manifest, select
from wrally.media import same_image
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("shot", "local", "remote", "result")


@dataclass
class VerifyReport:
    rows: list[tuple[str, ...]] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    errors: int = 0

    @property
    def summary(self) -> str:
        ok = sum(r[3].startswith("ok") for r in self.rows)
        unset = sum(r[3] in ("unset", "no pick") for r in self.rows)
        return f"verified picks: {ok} ok, {len(self.mismatches)} mismatch, {unset} not comparable"


def _existing(directory: Path, stem: str) -> Path | None:
    return next(iter(sorted(directory.glob(f"{stem}.*"))), None)


async def same_assets(
    ws: Workspace, client: McpClient, pairs: list[tuple[str, str]], http: httpx2.AsyncClient | None = None
) -> dict[tuple[str, str], bool | str]:
    """逐对判断两个 asset_id 是不是同一张图：下载到 .wrally/verify/ 后做像素比对。下载失败时值是错误码。"""
    directory = ws.state_dir / "verify"
    wanted = dict.fromkeys(a for pair in pairs for a in pair)
    plan = [(a, directory / a) for a in wanted if not _existing(directory, a)]
    downloads = await download_assets(client, plan, raw_path=ws.raw_dir / "verify-assets.json", http=http)
    failed = {d.asset_id: d.error for d in downloads if not d.ok}
    out: dict[tuple[str, str], bool | str] = {}
    for a, b in pairs:
        bad = next((x for x in (a, b) if x in failed), None)
        out[(a, b)] = failed[bad] if bad else same_image(_existing(directory, a), _existing(directory, b))
    return out


async def verify_picks(ws: Workspace, client: McpClient, spec: str = "all",
                       http: httpx2.AsyncClient | None = None) -> VerifyReport:  # fmt: skip
    data = load_manifest(ws)
    entries = select(data, spec)
    remote = {s.story_id: s for s in await shot_list(client, data["series_id"], data["project_id"],
                                                    raw_dir=ws.raw_dir, label="verify")}  # fmt: skip
    report = VerifyReport()
    pending: list[tuple[str, str, str]] = []  # 需要像素比对的 (key, local_id, remote_id)
    result: dict[str, tuple[str, str, str]] = {}
    for entry in entries:
        key = str(entry["key"])
        local = (entry.get("keyframe") or {}).get("asset_id")
        shot = remote.get(entry["story_id"])
        if shot is None:
            report.errors += 1
            result[key] = (str(local or "-"), "-", "error: missing on remote")
        elif not local:
            result[key] = ("-", str(shot.keyframe_asset_id or "-"), "no pick")
        elif not shot.keyframe_asset_id:
            result[key] = (local, "-", "unset")
        elif shot.keyframe_asset_id == local:
            result[key] = (local, local, "ok")
        else:
            pending.append((key, local, shot.keyframe_asset_id))

    same = await same_assets(ws, client, [(lo, re) for _, lo, re in pending], http)
    for key, local, other in pending:
        verdict = same[(local, other)]
        if isinstance(verdict, str):
            report.errors += 1
            result[key] = (local, other, f"error: {verdict}")
        elif verdict:
            result[key] = (local, other, "ok (same pixels)")
        else:
            report.mismatches.append(key)
            result[key] = (local, other, "MISMATCH")

    report.rows = [(str(e["key"]), *result[str(e["key"])]) for e in entries]
    report.rows.sort(key=lambda r: r[3].startswith("ok") or r[3] in ("unset", "no pick"))
    return report
