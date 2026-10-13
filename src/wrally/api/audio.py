"""旁白生成：绑定音色、提交。

调用顺序固定：逐镜 bind_asset 音色 → 批量 edit_content 写 audio_prompt → 一次 set_model audio → 提交。
audio_prompt 里用 <音色名> 引用绑定的音色，所以绑定时的 name 必须和提示词里的名字一致。
"""

from pathlib import Path

import httpx2

from wrally.api.assets import asset_details, plain_url
from wrally.errors import ToolError
from wrally.transport.client import McpClient


async def bind_voice(
    client: McpClient,
    items: list[tuple[str, str]],
    voice_asset: str,
    voice_name: str,
    raw_dir: Path | None = None,
    http: httpx2.AsyncClient | None = None,
) -> dict[str, str]:
    """给若干场次绑定音色（替换原有绑定）。items 是 (镜号, story_id)；返回 镜号 → 错误码（空串表示成功）。"""
    if not items:
        return {}
    asset = (await asset_details(client, [voice_asset])).get(voice_asset)
    if asset is None or not asset.url:
        return {key: "voice_asset_not_found" for key, _ in items}
    owned = http is None
    http = http or httpx2.AsyncClient(timeout=30)
    try:
        url = await plain_url(http, asset)
    except httpx2.TransportError:
        return {key: "network_error" for key, _ in items}
    finally:
        if owned:
            await http.aclose()
    out: dict[str, str] = {}
    for key, story_id in items:
        binding = {
            "story_id": story_id,
            "asset_type": "audio",
            "mode": "replace",
            "assets": [{"asset_id": voice_asset, "role_id": voice_asset, "name": voice_name, "url": url}],
        }
        try:
            await client.call(
                "shotlist_manage",
                {"action": "bind_asset", "binding": binding},
                raw_path=raw_dir / f"{key}-audio-bind_asset.json" if raw_dir else None,
            )
            out[key] = ""
        except ToolError as e:
            out[key] = e.code
    return out


async def submit_audio(
    client: McpClient, project_id: str, series_id: str, story_ids: list[str], raw_path: Path | None = None
) -> dict:
    """提交旁白生成任务（每镜一条），返回 {task_ids, skipped}。不自动重试。"""
    data = await client.call(
        "shotlist_generate_audio",
        {"project_id": project_id, "series_id": series_id, "story_ids": story_ids, "count": 1},
        raw_path=raw_path,
        retry=False,
    )
    return {"task_ids": list(data.get("task_ids") or []), "skipped": data.get("skipped") or []}
