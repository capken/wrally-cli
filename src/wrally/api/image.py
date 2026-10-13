"""关键帧生成：场次生图，以及带参考图的画布图生图。"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from wrally.api.assets import asset_details, plain_url
from wrally.errors import ToolError
from wrally.transport.client import McpClient


async def submit_images(
    client: McpClient,
    project_id: str,
    series_id: str,
    story_ids: list[str],
    count: int,
    raw_path: Path | None = None,
) -> dict:
    """为场次提交生图任务，返回 {task_ids, skipped}。不自动重试。生成前场次要已配好 image 模型和提示词。"""
    data = await client.call(
        "shotlist_generate_image",
        {"project_id": project_id, "series_id": series_id, "story_ids": story_ids, "count": count},
        raw_path=raw_path,
        retry=False,
    )
    return {"task_ids": list(data.get("task_ids") or []), "skipped": data.get("skipped") or []}


async def canvas_generate(
    client: McpClient,
    *,
    prompt: str,
    model: str,
    ref_asset_ids: list[str],
    project_id: str,
    name: str,
    count: int = 1,
    aspect: str | None = None,
    resolution: int | None = None,
    raw_path: Path | None = None,
    http: httpx2.AsyncClient | None = None,
) -> list[str]:
    """画布图生图：用若干已有图片作参考，返回任务 ID（每张图一个任务）。不自动重试。

    参考图按 ref_asset_ids 的顺序传入，提示词里用「第一张图片」「第二张图片」引用对应位置。
    """
    details = await asset_details(client, ref_asset_ids)
    missing = [a for a in ref_asset_ids if a not in details or not details[a].url]
    if missing:
        raise ToolError("ref_asset_not_found", tool="canvas_generate_image", raw_path=raw_path)
    owned = http is None
    http = http or httpx2.AsyncClient(timeout=30)
    try:
        urls = [await plain_url(http, details[a]) for a in ref_asset_ids]
    finally:
        if owned:
            await http.aclose()
    arguments = {
        "prompt": prompt,
        "model": model,
        "input_images": urls,
        "count": count,
        "short_series_project_id": project_id,  # 不传画布 ID：图片自动入库到媒资，不挂在某个画布上
        "task_name": name,
    }
    if aspect:
        arguments["aspect_ratio"] = aspect
    if resolution is not None:
        arguments["resolution"] = resolution
    data = await client.call("canvas_generate_image", arguments, raw_path=raw_path, retry=False)
    return [str(t) for t in data.get("task_ids") or []]


@dataclass
class CanvasTask:
    state: str  # running / done / failed
    asset_ids: list[str] = field(default_factory=list)


async def canvas_task(
    client: McpClient, task_id: str, project_id: str | None = None, raw_path: Path | None = None
) -> CanvasTask:
    arguments = {"task_id": task_id} | ({"project_id": project_id} if project_id else {})
    data = await client.call("canvas_get_task", arguments, raw_path=raw_path)
    status = str(data.get("status") or "").lower()
    code = data.get("state")
    if status == "success" or code == 4:
        return CanvasTask("done", [a["asset_id"] for a in data.get("assets") or [] if a.get("asset_id")])
    if status in ("failed", "cancelled", "canceled", "error") or code in (5, 6):
        return CanvasTask("failed")
    return CanvasTask("running")
