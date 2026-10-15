"""new：在 WorkRally 上新建剧集和场次，并建好本地清单。

可以重跑：清单里已有 series_id 就不再建剧集，只补远端还没有的场次。所以它也用来给已有的一集加镜头。
新场次的关键帧和动画提示词都是占位文本（服务端不接受空场次），建完后按 pull 的规则写入清单和 prompts/。
清单里可以先手写好条目和提示词再跑 new：没有 story_id 的条目按镜号认领，本地提示词保留，之后 push。
"""

from dataclasses import dataclass, field

from wrally.api.models import SPECIAL_KEYS
from wrally.api.series import series_create, series_list
from wrally.api.shots import add_shots, shot_list
from wrally.commands import pull as pull_cmd
from wrally.errors import PreflightError, ToolError
from wrally.prompts import FILE_FIELD, PLACEHOLDER
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("shot", "remote", "state")
# 清单里的 key → 远端的 story_num
STORY_NUMS = {key: num for num, key in SPECIAL_KEYS.items()}


@dataclass
class NewReport:
    series_id: str | None = None
    series_state: str = "exists"  # created / exists / would create
    rows: list[tuple[str, ...]] = field(default_factory=list)
    dry_run: bool = False
    placeholders: int = 0  # 本地还是占位文本的提示词
    kept_local: int = 0  # 本地已写好、和远端不同的提示词（待 push）

    @property
    def added(self) -> int:
        return sum(r[2] in ("added", "would add") for r in self.rows)

    @property
    def summary(self) -> str:
        verb = "would add" if self.dry_run else "added"
        tail = "" if self.dry_run else "  -> episode.yaml"
        return (
            f"series {self.series_id or '-'} ({self.series_state}); "
            f"{verb} {self.added} shots, {len(self.rows) - self.added} already there{tail}"
        )


def _check_taken(data: dict, missing: list[str]) -> None:
    """建场次前查清单，免得建完了 merge 才报错：要建的镜号不能已被清单里别的场次占着。"""
    entries = data.get("shots") or []
    taken = [str(e["key"]) for e in entries if e.get("story_id") and str(e.get("key")) in missing]
    if taken:
        raise PreflightError(
            "duplicate_shot_key", f"({','.join(taken)} already has a story_id in episode.yaml)"
        )


def _placeholders(ws: Workspace, keys: list[str]) -> int:
    data = ws.load() or {}
    count = 0
    for entry in data.get("shots") or []:
        if str(entry.get("key")) not in keys:
            continue
        for kind in ("image", "anim"):
            rel = entry.get(FILE_FIELD[kind])
            count += bool(rel) and (ws.read_text(str(rel)) or "").strip() == PLACEHOLDER
    return count


def expand_keys(spec: str) -> list[str]:
    """要建的镜号：00-11,99 → 00、01 … 11、99。只接受数字；00 是片头，99 是片尾。"""
    keys: list[str] = []
    for part in spec.split(","):
        lo, dash, hi = (x.strip() for x in part.partition("-"))
        hi = hi if dash else lo
        if not (lo.isdigit() and hi.isdigit()) or int(hi) < int(lo):
            raise PreflightError("bad_shots", f"({part.strip() or spec}; e.g. 00-11,99)")
        keys.extend(str(n).zfill(2) for n in range(int(lo), int(hi) + 1))
    if len(set(keys)) != len(keys):
        raise PreflightError("bad_shots", f"({spec}; duplicate shot)")
    return keys


async def new(
    ws: Workspace,
    client: McpClient,
    spec: str,
    *,
    project_id: str | None = None,
    name: str | None = None,
    series_id: str | None = None,
    title: str | None = None,
    dry_run: bool = False,
) -> NewReport:
    data = ws.load() or {}
    for field_name, given in (("project_id", project_id), ("series_id", series_id)):
        if given and data.get(field_name) not in (None, given):
            raise PreflightError("series_mismatch", f"({field_name} in episode.yaml differs)")
    project_id = project_id or data.get("project_id") or ws.project().get("project_id")
    series_id = series_id or data.get("series_id")
    if not project_id:
        raise PreflightError("missing_project", "(new needs --project, or project_id in wrally.yaml)")
    keys = expand_keys(spec)
    pull_cmd.check_manifest_keys(data.get("shots") or [])
    report = NewReport(series_id=series_id, dry_run=dry_run)

    existing = []
    if series_id:
        existing = await shot_list(client, series_id, project_id, raw_dir=ws.raw_dir, label="new")
        _check_taken(data, [k for k in keys if k not in {s.key for s in existing}])
    else:
        _check_taken(data, keys)
        if not name:
            raise PreflightError("missing_name", "(new series needs --name)")
        # 同名剧集多半是上次建到一半留下的，不替人决定是否沿用
        same = [s for s in await series_list(client, project_id, raw_dir=ws.raw_dir) if s.name == name]
        if same:
            raise PreflightError("series_exists", f"({same[0].series_id}; pass --series to reuse it)")
        report.series_state = "would create"
        if not dry_run:
            series_id = await series_create(
                client, project_id, name, raw_path=ws.raw_dir / "new-series_create.json"
            )
            if series_id is None:
                made = [s for s in await series_list(client, project_id) if s.name == name]
                if len(made) != 1:
                    raise ToolError(
                        "series_id_not_found",
                        tool="series_create",
                        raw_path=ws.raw_dir / "new-series_create.json",
                    )
                series_id = made[0].series_id
            report.series_id, report.series_state = series_id, "created"
            # 先把剧集 ID 落盘：建场次那一步失败后，重跑能接着补，不会再建一个剧集
            pull_cmd.merge(ws, [], project_id=project_id, series_id=series_id, title=title)

    remote_keys = {s.key for s in existing}
    missing = [k for k in keys if k not in remote_keys]
    if missing and not dry_run:
        assert series_id is not None
        last = max((s.sort_num for s in existing), default=0)
        await add_shots(
            client,
            series_id,
            project_id,
            [
                {
                    "story_num": STORY_NUMS.get(k, k),
                    "sort_num": last + n,
                    "image_prompt": PLACEHOLDER,
                    "animation_prompt": PLACEHOLDER,
                }
                for n, k in enumerate(missing, 1)
            ],
            raw_path=ws.raw_dir / "new-add.json",
        )
        shots = await shot_list(client, series_id, project_id, raw_dir=ws.raw_dir, label="new")
        merged = pull_cmd.merge(ws, shots, project_id=project_id, series_id=series_id, title=title)
        report.kept_local = merged.kept_local
        report.placeholders = _placeholders(ws, missing)
        lost = [k for k in missing if k not in {s.key for s in shots}]
        if lost:
            raise ToolError("shots_not_created", tool="shotlist_manage", raw_path=ws.raw_dir / "new-add.json")
    elif series_id and not dry_run:
        pull_cmd.merge(ws, existing, project_id=project_id, series_id=series_id, title=title)

    state = "would add" if dry_run else "added"
    report.rows = [(k, STORY_NUMS.get(k, k), state if k in missing else "exists") for k in keys]
    report.rows.sort(key=lambda r: r[2] == "exists")
    return report
