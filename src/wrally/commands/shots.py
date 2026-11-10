"""shots rm / shots order：删场次、按清单重排场次。

rm：远端软删除（进回收站），清单里去掉这几镜。被去掉的条目和它的提示词文件挪到
.wrally/removed/<镜号>-<story_id>/，不直接删；已下载的素材不动。远端已经没有的（上次删到一半）只改本地。
order：让网页上的场次顺序和清单一致；--by-key 先把清单按镜号排好。远端和清单的场次要一一对应。
"""

import shutil
from dataclasses import dataclass, field

from ruamel.yaml.comments import CommentedMap

from wrally.api import shots as shots_api
from wrally.commands.common import load_manifest, parse_shots, remember_web_copies
from wrally.errors import PreflightError, ToolError
from wrally.prompts import FILE_FIELD
from wrally.transport.client import McpClient
from wrally.workspace import Workspace, to_yaml, write_if_changed

RM_HEADERS = ("shot", "story_id", "state")
ORDER_HEADERS = ("shot", "from", "to")
REMOVED = "removed"  # 在 .wrally/ 下


@dataclass
class ShotsReport:
    summary: str = ""
    rows: list[tuple[str, ...]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _keys(entries: list) -> str:
    return ",".join(str(e["key"]) for e in entries)


def _archive(ws: Workspace, entry: CommentedMap) -> str:
    """把条目和它的提示词文件挪进 .wrally/removed/，返回那个目录（相对单集目录）。"""
    folder = ws.state_dir / REMOVED / f"{entry['key']}-{entry.get('story_id') or 'local'}"
    folder.mkdir(parents=True, exist_ok=True)
    for name in FILE_FIELD.values():
        rel = entry.get(name)
        src = ws.root / str(rel) if rel else None
        if src and src.is_file():
            shutil.move(str(src), str(folder / src.name))
    write_if_changed(folder / "entry.yaml", to_yaml(entry))
    return str(folder.relative_to(ws.root))


async def rm(ws: Workspace, client: McpClient, spec: str, *, dry_run: bool = False) -> ShotsReport:
    data = load_manifest(ws)
    if spec.strip() == "all":
        raise PreflightError("bad_shots", "(name the shots to remove, e.g. 07,08)")
    entries = list(data["shots"])
    keys = parse_shots(spec, [str(e["key"]) for e in entries])
    chosen = [e for e in entries if str(e["key"]) in keys]
    series_id, project_id = data["series_id"], data["project_id"]
    remote = await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "shots-rm")
    remember_web_copies(ws, remote)
    remote_ids = {s.story_id for s in remote}
    on_remote = [e for e in chosen if e.get("story_id") in remote_ids]
    report = ShotsReport()
    # 别的镜头拿这几镜当参考图：删了之后它们的 refs 就找不到了
    users = [
        str(e["key"])
        for e in entries
        if e not in chosen and set(map(str, (e.get("keyframe") or {}).get("refs") or [])) & set(keys)
    ]
    if users:
        report.notes.append(f"note: {','.join(users)} use these shots as refs; edit their keyframe.refs")
    if dry_run:
        report.rows = [
            (str(e["key"]), str(e.get("story_id") or "-"),
             "would delete" if e in on_remote else "would drop locally (not on remote)")
            for e in chosen
        ]  # fmt: skip
        report.summary = (
            f"dry run: would delete {len(on_remote)} shots on WorkRally, drop {len(chosen)} locally"
        )
        return report
    if on_remote:
        raw = ws.raw_dir / "shots-delete.json"
        await shots_api.delete_shots(client, series_id, project_id, [e["story_id"] for e in on_remote], raw)
        after = await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "shots-rm-verify")
        remember_web_copies(ws, after)
        left = {s.story_id for s in after}
        kept = [e for e in on_remote if e["story_id"] in left]
        if kept:
            raise ToolError("shots_not_deleted", tool="shotlist_manage", raw_path=raw)
    for e in chosen:
        folder = _archive(ws, e)
        data["shots"].remove(e)
        state = "deleted" if e in on_remote else "dropped locally"
        report.rows.append((str(e["key"]), str(e.get("story_id") or "-"), f"{state}  -> {folder}"))
    ws.save(data)
    report.summary = f"removed {len(chosen)} shots ({len(on_remote)} deleted on WorkRally)  -> episode.yaml"
    return report


def _key_order(entry: CommentedMap) -> tuple:
    key = str(entry["key"])
    return (0, int(key), key) if key.isdigit() else (1, 0, key)


async def order(
    ws: Workspace, client: McpClient, *, by_key: bool = False, dry_run: bool = False
) -> ShotsReport:
    data = load_manifest(ws)
    entries = list(data["shots"])
    wanted = sorted(entries, key=_key_order) if by_key else entries
    local_only = [e for e in wanted if not e.get("story_id")]
    if local_only:
        raise PreflightError("not_on_remote", f"({_keys(local_only)}; run: wrally new)")
    series_id, project_id = data["series_id"], data["project_id"]
    remote = await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "shots-order")
    remember_web_copies(ws, remote)
    remote_ids = [s.story_id for s in remote]
    missing = [e for e in wanted if e["story_id"] not in remote_ids]
    if missing:
        raise PreflightError("missing_on_remote", f"({_keys(missing)}; run: wrally pull)")
    ids = [e["story_id"] for e in wanted]
    extra = [s.key for s in remote if s.story_id not in ids]
    if extra:
        raise PreflightError("not_in_manifest", f"({','.join(extra)} on WorkRally only; run: wrally pull)")
    report = ShotsReport()
    report.rows = [
        (str(e["key"]), str(remote_ids.index(e["story_id"]) + 1), str(n))
        for n, e in enumerate(wanted, 1)
        if remote_ids.index(e["story_id"]) + 1 != n
    ]
    manifest_moves = wanted != entries
    if manifest_moves:
        # 清单顺序就是成片顺序（export、page 都按它）；镜号只能是数字，插进来的镜头常常故意不按镜号排
        report.notes.append("note: --by-key reorders episode.yaml; export and page.html follow that order")
    if dry_run:
        tail = "; episode.yaml would be sorted by key" if manifest_moves else ""
        report.summary = f"dry run: would move {len(report.rows)} shots on WorkRally{tail}"
        return report
    if manifest_moves:
        data["shots"][:] = wanted
        ws.save(data)
    if not report.rows:
        report.summary = "WorkRally order already matches episode.yaml" + (
            " (episode.yaml sorted by key)" if manifest_moves else ""
        )
        return report
    raw = ws.raw_dir / "shots-sort.json"
    await shots_api.sort_shots(client, series_id, project_id, ids, raw)
    after = await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "shots-order-verify")
    remember_web_copies(ws, after)
    if [s.story_id for s in after] != ids:
        raise ToolError("order_not_applied", tool="shotlist_manage", raw_path=raw)
    report.summary = f"moved {len(report.rows)} shots on WorkRally to match episode.yaml"
    return report
