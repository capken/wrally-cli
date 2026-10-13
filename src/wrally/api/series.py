"""剧集接口：列出项目下的剧集、新建剧集。"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from wrally.transport.client import McpClient

PAGE_SIZE = 50


class Series(BaseModel):
    series_id: str
    name: str = ""


async def series_list(client: McpClient, project_id: str, raw_dir: Path | None = None) -> list[Series]:
    """项目下的全部剧集。返回里的封面地址不保留。"""
    found: list[Series] = []
    page = 1
    while True:
        data = await client.call(
            "series_list",
            {"project_id": project_id, "page": page, "page_size": PAGE_SIZE},
            raw_path=raw_dir / f"new-series_list-p{page}.json" if raw_dir else None,
        )
        items = data.get("series_list") or []
        found.extend(Series(series_id=s["series_id"], name=str(s.get("series_name") or "")) for s in items)
        total = int((data.get("page_info") or {}).get("total") or 0)
        if not items or len(found) >= total:
            break
        page += 1
    return found


def _find_id(node: Any) -> str | None:
    if isinstance(node, dict):
        if isinstance(node.get("series_id"), str) and node["series_id"]:
            return node["series_id"]
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            if found := _find_id(child):
                return found
    return None


async def series_create(
    client: McpClient, project_id: str, name: str, raw_path: Path | None = None
) -> str | None:
    """新建剧集，返回 series_id；返回体里找不到 ID 时返回 None，由调用方按名字到列表里找。

    不自动重试：超时后无法判断服务端是否已受理，重发会建出两个同名剧集。
    """
    data = await client.call(
        "series_create", {"project_id": project_id, "name": name}, raw_path=raw_path, retry=False
    )
    return _find_id(data)
