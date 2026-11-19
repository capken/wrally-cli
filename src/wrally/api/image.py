"""关键帧生成：场次生图与画布任务查询（画布提交统一在 api/canvas.py）。"""

import re
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class CanvasTask:
    state: str  # running / done / failed
    asset_ids: list[str] = field(default_factory=list)
    error: str | None = None  # 失败时服务端给的原因（如「现在使用的人比较多」）


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
        return CanvasTask("failed", error=_error_message(data.get("error")))
    return CanvasTask("running")


def _error_message(error: object, limit: int = 60) -> str | None:
    """取出失败原因的一行文字；不含地址。"""
    if isinstance(error, dict):
        error = error.get("message") or error.get("msg")
    if not isinstance(error, str):
        return None
    text = " ".join(re.sub(r"https?://\S+", "<url>", error).split())
    return (text[: limit - 1] + "…" if len(text) > limit else text) or None
