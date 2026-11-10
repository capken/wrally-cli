"""视频生成：单图模式、首尾帧模式的配置与提交。

单图模式在配置里叫 mode=Text，必须和 textModel、singleAssets 一起完整写入；
singleAssets 必须带 asset_id，否则视频不参考关键帧。
首尾帧模式叫 mode=FirstLastFrame，模型写 firstLastModel，两张图按 [首帧, 尾帧] 写进 firstLastAssets。
singleAssets 照样写首帧：网页「选用」的关键帧从它读，选图冲突的检查靠它。
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
    firstlast_model: str | None = None  # 首尾帧模式用的模型；不参与输入指纹，见 commands/video.fingerprint


def _asset_item(asset_id: str, name: str, url: str) -> dict:
    return {"asset_id": asset_id, "assetId": asset_id, "fileType": "image", "name": name, "url": url}


def single_image_config(settings: VideoSettings, asset_id: str, name: str, url: str) -> dict:
    return {
        "mode": "Text",
        "extraMode": None,
        "model": settings.model,
        "textModel": settings.model,
        "singleAssets": [_asset_item(asset_id, name, url)],
        "aspectRatio": settings.aspect,
        "duration": settings.duration,
        "resolution": settings.resolution,
        "generateAudio": settings.audio,
    }


Frame = tuple[str, str, str]  # (asset_id, 名字, 固定地址)


def first_last_config(settings: VideoSettings, first: Frame, last: Frame) -> dict:
    """settings.model 已经是首尾帧模型（shot_settings 换好）。"""
    config = single_image_config(settings, *first)
    config["mode"] = "FirstLastFrame"
    config["firstLastModel"] = settings.model
    config["firstLastAssets"] = [_asset_item(*first), _asset_item(*last)]
    return config


async def configure(
    client: McpClient,
    series_id: str,
    project_id: str,
    items: list[tuple[str, str, str, str | None]],
    settings: dict[str, VideoSettings],
    raw_dir: Path | None = None,
    http: httpx2.AsyncClient | None = None,
) -> dict[str, str]:
    """逐镜写入视频配置。items 是 (镜号, story_id, 关键帧 asset_id, 尾帧 asset_id 或 None)，
    有尾帧的写首尾帧模式，没有的写单图模式；settings 按镜号给出各镜的参数。

    返回 镜号 → 错误码（空串表示成功）。
    """
    wanted = list(dict.fromkeys([a for _, _, a, _ in items] + [b for *_, b in items if b]))
    details = await asset_details(client, wanted, raw_dir / "video-assets.json" if raw_dir else None)
    owned = http is None
    http = http or httpx2.AsyncClient(timeout=30)
    out: dict[str, str] = {}

    async def frame(key: str, asset_id: str, label: str) -> tuple[str, str, str]:
        asset = details[asset_id]
        return asset_id, asset.title or f"{key}{label}{asset.suffix}", await plain_url(http, asset)

    try:
        for key, story_id, asset_id, last_id in items:
            if any(details.get(a) is None or not details[a].url for a in (asset_id, last_id) if a):
                out[key] = "asset_not_found"
                continue
            try:
                first = await frame(key, asset_id, "")
                if last_id:
                    config = first_last_config(settings[key], first, await frame(key, last_id, "-last"))
                else:
                    config = single_image_config(settings[key], *first)
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
