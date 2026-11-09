"""短番项目接口：列出当前用户的项目、新建项目。

project_list 的筛选：不传（等于 0）是全部；1「我创建的」会连回收站里的也带上（2026-11-09 真实看到），
所以列全部项目不传筛选。返回里的 series_count 等计数恒为 0，不可信，剧集数要另用 series_list 数。
"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from wrally.transport.client import McpClient

PAGE_SIZE = 50
TRASH = 11  # 回收站


class Project(BaseModel):
    project_id: str
    name: str = ""
    created: str = ""  # YYYY-MM-DD
    updated: str = ""


async def project_list(
    client: McpClient, *, search: str | None = None, trash: bool = False, raw_dir: Path | None = None
) -> list[Project]:
    """全部项目（按远端顺序，新建的在前）。封面地址不保留。"""
    found: list[Project] = []
    page = 1
    while True:
        args: dict = {"page": page, "page_size": PAGE_SIZE}
        if search:
            args["search_key"] = search
        if trash:
            args["filter"] = TRASH
        data = await client.call(
            "project_list", args, raw_path=raw_dir / f"project_list-p{page}.json" if raw_dir else None
        )
        items = data.get("project_list") or []
        found.extend(
            Project(
                project_id=p["project_id"],
                name=str(p.get("project_name") or ""),
                created=str(p.get("created_at") or "")[:10],
                updated=str(p.get("updated_at") or "")[:10],
            )
            for p in items
            if p.get("project_id")
        )
        total = int((data.get("page_info") or {}).get("total") or 0)
        if not items or len(found) >= total:
            break
        page += 1
    return found


def _find_id(node: Any) -> str | None:
    if isinstance(node, dict):
        if isinstance(node.get("project_id"), str) and node["project_id"]:
            return node["project_id"]
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            if found := _find_id(child):
                return found
    return None


async def project_create(
    client: McpClient, name: str, *, canvas: str | None = None, raw_path: Path | None = None
) -> str | None:
    """新建项目，返回 project_id；返回体里找不到 ID 时返回 None，由调用方按名字到列表里找。

    不自动建剧集（默认会建一个空剧集，wrally new 再建就多出一个）。
    不自动重试：超时后无法判断服务端是否已受理，重发会建出两个同名项目。
    """
    args: dict = {"project_name": name, "auto_create_series": False, "series_count": 0}
    if canvas:
        args["canvas_size_value"] = canvas
    data = await client.call("project_create", args, raw_path=raw_path, retry=False)
    return _find_id(data)
