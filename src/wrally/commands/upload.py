"""upload：把本地文件上传成媒资；其他命令遇到写成本地路径的参考图、首尾帧、音色时也走这里。

能写本地路径的地方：`gen` 的 --ref、--image、--last、--voice，清单或项目文件的 ref_assets 各项，
defaults.audio.voice_asset。值里带扩展名、斜杠或 ~ 的算路径，否则算媒资 ID。
同一个文件在同一个项目里只传一次（记录在凭证目录的 uploads.json）。
"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from wrally import localfile
from wrally.api import upload as upload_api
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("file", "asset_id", "state")


@dataclass
class Resolved:
    """值 → 媒资 ID。dry run 时没传过的文件对应 None。"""

    ids: dict[str, str | None] = field(default_factory=dict)
    uploaded: int = 0
    pending: int = 0  # dry run：会上传几个

    def note(self) -> str:
        if self.pending:
            return f"would upload {self.pending} local file(s)"
        return f"uploaded {self.uploaded} local file(s)" if self.uploaded else ""


async def resolve(
    client: McpClient,
    values: list[str],
    base: Path,
    project_id: str | None,
    *,
    dry_run: bool = False,
    http: httpx2.AsyncClient | None = None,
) -> Resolved:
    """把一批值里的本地路径换成媒资 ID，媒资 ID 原样返回。

    dry run 不上传：传过的用本地记录里的 ID，没传过的给 None。没给项目时归到「默认项目」。
    """
    out = Resolved()
    paths: dict[str, Path] = {}
    for value in dict.fromkeys(values):
        if localfile.is_local(value):
            paths[value] = localfile.local_path(value, base)
            localfile.check_file(paths[value])
        else:
            out.ids[value] = value
    if not paths:
        return out
    if dry_run:
        for value, path in paths.items():
            out.ids[value] = upload_api.cached(path, project_id)
            out.pending += out.ids[value] is None
        return out
    project_id = project_id or await upload_api.default_project(client)
    done = await upload_api.upload(client, list(paths.values()), project_id, http=http)
    for value, got in zip(paths, done, strict=True):
        out.ids[value] = got.asset_id
        out.uploaded += got.state == "uploaded"
    return out


def print_of(ws: Workspace, value: object) -> str:
    """进输入指纹的写法：媒资 ID 原样，本地文件按内容（换了路径不算变，换了内容才算）。不调远端。"""
    path = ws.media_file(value)
    if path is None:
        return str(value)
    localfile.check_file(path)
    return localfile.file_print(path)


def project_of(directory: Path) -> str | None:
    """目录所属的项目：单集清单的 project_id，再取项目文件的。"""
    ws = Workspace(directory)
    data = ws.load() or {}
    return data.get("project_id") or ws.project().get("project_id")


async def run(
    client: McpClient,
    files: list[str],
    *,
    project_id: str | None,
    directory: Path,
    dry_run: bool = False,
    http: httpx2.AsyncClient | None = None,
) -> tuple[str, list[tuple[str, str, str]]]:
    """wrally upload：返回 (一行结论, 表格)。"""
    paths = [localfile.local_path(f, Path(".")) for f in files]
    for path in paths:
        localfile.check_file(path)
    project_id = project_id or project_of(directory)
    if dry_run:
        rows = []
        for f, path in zip(files, paths, strict=True):
            hit = upload_api.cached(path, project_id)
            rows.append((f, hit or "-", "uploaded before" if hit else "would upload"))
        todo = sum(r[2] == "would upload" for r in rows)
        where = project_id or upload_api.DEFAULT_PROJECT
        return f"dry run: would upload {todo} of {len(files)} file(s) to project {where}", rows
    project_id = project_id or await upload_api.default_project(client)
    done = await upload_api.upload(client, paths, project_id, http=http)
    rows = [(f, d.asset_id, d.state) for f, d in zip(files, done, strict=True)]
    made = sum(d.state == "uploaded" for d in done)
    return f"{made} uploaded, {len(done) - made} already there  (project {project_id})", rows
