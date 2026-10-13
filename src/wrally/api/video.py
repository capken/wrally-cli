"""视频生成：单图模式的配置与提交。

单图模式在配置里叫 mode=Text，必须和 textModel、singleAssets 一起完整写入；
singleAssets 必须带 asset_id，否则视频不参考关键帧。
"""

from dataclasses import dataclass
from pathlib import Path

import httpx2

from wrally.api.assets import asset_details, plain_url
from wrally.api.shots import set_model
from wrally.errors import ToolError
from wrally.transport.client import McpClient


@dataclass(frozen=True)
class VideoSettings:
    model: str
    aspect: str = "16:9"
    duration: int = 8
    resolution: int = 1
    audio: bool = False


def single_image_config(settings: VideoSettings, asset_id: str, name: str, url: str) -> dict:
    return {
        "mode": "Text",
        "extraMode": None,
        "model": settings.model,
        "textModel": settings.model,
        "singleAssets": [
            {"asset_id": asset_id, "assetId": asset_id, "fileType": "image", "name": name, "url": url}
        ],
        "aspectRatio": settings.aspect,
        "duration": settings.duration,
        "resolution": settings.resolution,
        "generateAudio": settings.audio,
    }


async def configure_single_image(
    client: McpClient,
    series_id: str,
    project_id: str,
    items: list[tuple[str, str, str]],
    settings: dict[str, VideoSettings],
    raw_dir: Path | None = None,
    http: httpx2.AsyncClient | None = None,
) -> dict[str, str]:
    """逐镜写入单图配置。items 是 (镜号, story_id, 关键帧 asset_id)，settings 按镜号给出各镜的参数。

    返回 镜号 → 错误码（空串表示成功）。
    """
    details = await asset_details(
        client, [a for _, _, a in items], raw_dir / "video-assets.json" if raw_dir else None
    )
    owned = http is None
    http = http or httpx2.AsyncClient(timeout=30)
    out: dict[str, str] = {}
    try:
        for key, story_id, asset_id in items:
            asset = details.get(asset_id)
            if asset is None or not asset.url:
                out[key] = "asset_not_found"
                continue
            try:
                url = await plain_url(http, asset)
                name = asset.title or f"{key}{asset.suffix}"
                config = single_image_config(settings[key], asset_id, name, url)
                raw = raw_dir / f"{key}-video-set_model.json" if raw_dir else None
                await set_model(client, series_id, project_id, [story_id], "video", config, raw)
                out[key] = ""
            except httpx2.TransportError:
                out[key] = "network_error"
            except ToolError as e:
                out[key] = e.code
    finally:
        if owned:
            await http.aclose()
    return out


async def submit_video(
    client: McpClient, project_id: str, series_id: str, story_ids: list[str], raw_path: Path | None = None
) -> dict:
    """提交生成任务，返回 {task_ids, skipped}。不自动重试：超时后重发可能重复消耗额度。"""
    data = await client.call(
        "shotlist_generate_video",
        {"project_id": project_id, "series_id": series_id, "story_ids": story_ids},
        raw_path=raw_path,
        retry=False,
    )
    return {"task_ids": list(data.get("task_ids") or []), "skipped": data.get("skipped") or []}
