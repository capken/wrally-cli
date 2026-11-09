"""projects：列出 WorkRally 上的短番项目；projects create 新建项目并在当前目录写出项目文件 wrally.yaml。

create 的顺序：本地检查 → 同名项目检查 → project_create（不自动建剧集、不重试）→ 写 wrally.yaml。
目录里已有 wrally.yaml 时不改它：已经写着 project_id 的直接中止，没写的建完后提示把 ID 加进去。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

from wrally.api import projects as projects_api
from wrally.api.series import series_list
from wrally.commands.common import for_each
from wrally.errors import NetworkError, PreflightError, ToolError, WrallyError
from wrally.transport.client import McpClient
from wrally.workspace import MANIFEST, PROJECT, Workspace, write_if_changed

LIST_HEADERS = ("id", "name", "episodes", "created", "updated", "here")
CREATE_HEADERS = ("project_id", "name", "file")
CANVAS = re.compile(r"^\d+:\d+$")


def local_project_id(directory: Path) -> str:
    """当前目录所属的项目：单集清单里的，或这一级和上级目录的 wrally.yaml 里的。"""
    ws = Workspace(directory)
    found = (ws.load() or {}).get("project_id") or ws.project().get("project_id")
    if not found and (directory / PROJECT).is_file():
        loaded = YAML(typ="safe").load((directory / PROJECT).read_text(encoding="utf-8"))
        found = loaded.get("project_id") if isinstance(loaded, dict) else None
    return str(found or "")


async def listing(
    client: McpClient, directory: Path, *, search: str | None = None, trash: bool = False
) -> tuple[str, list[tuple[str, ...]]]:
    found = await projects_api.project_list(client, search=search, trash=trash)
    # 列表里的剧集数恒为 0，逐个项目数一下
    counts = await for_each(found, lambda p: series_list(client, p.project_id))
    here = local_project_id(directory)
    rows = [
        (p.project_id, p.name, "?" if isinstance(n, WrallyError) else str(len(n)), p.created, p.updated,
         "*" if p.project_id == here else "")
        for p, n in zip(found, counts, strict=True)
    ]  # fmt: skip
    line = f"{len(rows)} {'in trash' if trash else 'projects'}" + (f" matching {search!r}" if search else "")
    return line, rows


@dataclass
class CreateReport:
    name: str
    project_id: str | None = None
    dry_run: bool = False
    file_state: str = ""  # written / kept / would write / would keep
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.dry_run:
            return f"dry run: would create project {self.name!r}; {PROJECT} ({self.file_state})"
        return f"created project {self.project_id} {self.name!r}; {PROJECT} ({self.file_state})"

    @property
    def rows(self) -> list[tuple[str, ...]]:
        return [(self.project_id or "-", self.name, f"{PROJECT} ({self.file_state})")]


def project_file(project_id: str, name: str) -> str:
    """新项目的 wrally.yaml：project_id、title，加上要填的 defaults 骨架（注释掉，模型 ID 要向用户确认）。"""
    title = json.dumps(name, ensure_ascii=False)  # 双引号字符串也是合法的 YAML，名字里有冒号也不怕
    return "\n".join([
        "# 各集共用的设置。单集的 episode.yaml 只写与这里不同的部分：",
        "# 映射逐键合并，列表和单个值整个替换，写 null 表示这一集不要这一项。",
        "# 这里的路径相对本文件所在的目录。由 wrally projects create 生成，之后 wrally 只读它。",
        f"project_id: {project_id}",
        f"title: {title}",
        "",
        "# 生成前写进远端的模型配置；模型 ID 用 `wrally models <类别>` 查，向用户确认后再填。",
        "# defaults:",
        '#   image: {model: <图片模型 ID>, resolution: 4, aspect: "16:9", count: 2}',
        "#   video: {model: <视频模型 ID>, resolution: 1, duration: 8, audio: false}",
        "#   audio: {model: <旁白模型 ID>, voice_asset: <音色的媒资 ID>, voice_name: <音色名>}",
        "",
        "# check 和 lint 的阈值与词表，没写的一项就不查。",
        "# rules:",
        "#   narration_max: 30",
        "",
    ])  # fmt: skip


async def _find_by_name(client: McpClient, name: str) -> list[str]:
    return [p.project_id for p in await projects_api.project_list(client, search=name) if p.name == name]


async def create(
    client: McpClient,
    directory: Path,
    name: str,
    *,
    canvas: str | None = None,
    dry_run: bool = False,
) -> CreateReport:
    name = name.strip()
    if not name:
        raise PreflightError("missing_name", "(give the project a name)")
    if canvas and not CANVAS.match(canvas):
        raise PreflightError("bad_canvas", f"({canvas}; e.g. 1920:1080)")
    if (directory / MANIFEST).is_file():
        raise PreflightError("episode_directory", "(run it in the project directory, above the episodes)")
    path = directory / PROJECT
    existing = path.is_file()
    if existing:
        loaded = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and loaded.get("project_id"):
            raise PreflightError(
                "project_file_exists", f"({path} already has project_id {loaded['project_id']})"
            )
    report = CreateReport(name=name, dry_run=dry_run)
    # 同名项目多半是上次建到一半留下的，不替人决定是否沿用
    same = await _find_by_name(client, name)
    if same:
        raise PreflightError("project_exists", f"({same[0]}; put it in {PROJECT} as project_id to reuse it)")
    if dry_run:
        report.file_state = "would keep" if existing else "would write"
        return report

    raw = directory / ".wrally" / "raw" / "project_create.json"
    try:
        project_id = await projects_api.project_create(client, name, canvas=canvas, raw_path=raw)
    except (NetworkError, ToolError) as err:
        if isinstance(err, ToolError) and err.code != "bad_response":
            raise  # 服务端明确拒绝
        # 超时或返回体解析不了：不知道是否已建成，按名字找一次，找到唯一一个就当作建成了
        made = await _find_by_name(client, name)
        if len(made) != 1:
            raise
        project_id = made[0]
    if project_id is None:
        made = await _find_by_name(client, name)
        if len(made) != 1:
            raise ToolError("project_id_not_found", tool="project_create", raw_path=raw)
        project_id = made[0]
    report.project_id = project_id
    if existing:
        report.file_state = "kept"
        report.notes.append(
            f"note: {PROJECT} already exists and was not changed; add `project_id: {project_id}`"
        )
    else:
        write_if_changed(path, project_file(project_id, name))
        report.file_state = "written"
    return report
