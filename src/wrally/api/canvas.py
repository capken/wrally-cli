"""画布生成：不依赖项目、剧集和场次的生图、生视频、生音频和生音乐。

提交只发一次（retry=False），返回任务 ID；结果用 image.canvas_task 查询，用 assets.download_assets 下载。
参考图要传固定地址：由媒资 ID 经 asset_details + plain_url 换得，地址不出本模块。
"""

from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from wrally.api.assets import asset_details, plain_url
from wrally.errors import PreflightError
from wrally.transport.client import McpClient

KINDS = ("image", "video", "audio", "music")
VIDEO_MODES = {"text": "Text", "firstlast": "FirstLastFrame"}


@dataclass
class Model:
    id: str
    name: str
    resolutions: list[str] = field(default_factory=list)  # 分辨率枚举值，如 "4"
    aspects: list[str] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)  # 视频可选时长（秒）
    modes: list[str] = field(default_factory=list)  # 视频：支持的驱动模式（text / firstlast）
    sound: bool = False  # 视频：能否生成音效
    max_refs: int = 0  # 音频：最多几段参考音频
    minimax: bool = False  # 音频：MiniMax 模型不接受参考音频

    def matches(self, wanted: str) -> bool:
        return wanted == self.id or wanted.lower() == self.name.lower()


def _resolutions(entry: dict) -> list[str]:
    return [str(r.get("value")) for r in entry.get("resolution_options") or [] if r.get("value") is not None]


async def models(client: McpClient, kind: str, raw_path: Path | None = None) -> list[Model]:
    """某一类的可用模型。不同环境的模型和参数范围会变，所以每次从远端取，不写死。"""
    if kind == "image":
        data = await client.call("canvas_image_model_list", {}, raw_path=raw_path)
        common = data.get("aspect_ratios") or []
        return [
            Model(m["id"], m.get("name") or "", _resolutions(m), m.get("aspect_ratios") or common)
            for m in data.get("models") or []
        ]
    if kind == "video":
        data = await client.call("canvas_video_provider_config", {}, raw_path=raw_path)
        common = data.get("aspect_ratios") or []
        found: dict[str, Model] = {}
        for group in data.get("modes") or []:
            mode = next((k for k, v in VIDEO_MODES.items() if v == group.get("mode")), None)
            if mode is None:
                continue
            for m in group.get("models") or []:
                durations = [int(d) for d in m.get("duration_options") or []]
                sound = bool(m.get("support_audio"))
                model = found.setdefault(
                    m["id"],
                    Model(m["id"], m.get("name") or "", _resolutions(m), common, durations, sound=sound),
                )
                model.modes.append(mode)
        return list(found.values())
    data = await client.call("canvas_audio_model_list", {}, raw_path=raw_path)
    key = "music_models" if kind == "music" else "audio_models"
    return [
        Model(
            m["model_id"],
            m.get("name") or "",
            max_refs=int(
                (m.get("restrictions") or {}).get("max_audio_count") or m.get("max_audio_count") or 0
            ),
            minimax=bool(m.get("is_minimax")),
        )
        for m in data.get(key) or []
    ]


def pick_model(catalog: list[Model], wanted: str, kind: str) -> Model:
    """按 ID 或名称（不分大小写）找模型；找不到时报 unknown_model 并列出可用的名字。"""
    for m in catalog:
        if m.matches(wanted):
            return m
    names = ", ".join(m.name for m in catalog[:12])
    raise PreflightError("unknown_model", f"({kind}: {wanted}; try `wrally models {kind}`: {names} …)")


async def _image_urls(client: McpClient, asset_ids: list[str], http: httpx2.AsyncClient | None) -> list[str]:
    """媒资 ID → 固定地址，顺序不变。不存在的 ID 报 ref_asset_not_found。"""
    if not asset_ids:
        return []
    details = await asset_details(client, asset_ids)
    missing = [a for a in asset_ids if a not in details or not details[a].url]
    if missing:
        raise PreflightError("ref_asset_not_found", f"({','.join(missing)})")
    owned = http is None
    http = http or httpx2.AsyncClient(timeout=30)
    try:
        return [await plain_url(http, details[a]) for a in asset_ids]
    finally:
        if owned:
            await http.aclose()


def _task_ids(data: dict) -> list[str]:
    return [str(t) for t in data.get("task_ids") or []]


async def submit(
    client: McpClient,
    kind: str,
    params: dict,
    *,
    project_id: str | None = None,
    raw_path: Path | None = None,
    http: httpx2.AsyncClient | None = None,
) -> list[str]:
    """提交一次生成，返回任务 ID（每个产物一个）。不自动重试。

    params 是 commands/gen.py 整理好的参数：prompt、model、count，以及各类自己的
    aspect、resolution、duration、mode、image、last、sound、refs、voices、lyrics。
    """
    args: dict = {"prompt": params["prompt"], "model": params["model"], "count": params.get("count", 1)}
    if params.get("name"):
        args["task_name"] = params["name"]
    if project_id:
        args["short_series_project_id"] = project_id  # 只关联短番项目；不传画布 ID，不在画布上建节点
    if kind == "image":
        tool = "canvas_generate_image"
        if params.get("refs"):
            args["input_images"] = await _image_urls(client, params["refs"], http)
        for key, name in (("aspect", "aspect_ratio"), ("resolution", "resolution")):
            if params.get(key) is not None:
                args[name] = params[key]
    elif kind == "video":
        tool = "canvas_generate_video"
        mode = params.get("mode", "text")
        args["mode"] = VIDEO_MODES[mode]
        frames = [a for a in (params.get("image"), params.get("last")) if a]
        urls = dict(zip(frames, await _image_urls(client, frames, http), strict=True))
        if mode == "text" and params.get("image"):
            args["single_image_url"] = urls[params["image"]]
        if mode == "firstlast":
            if params.get("image"):
                args["first_frame_url"] = urls[params["image"]]
            if params.get("last"):
                args["last_frame_url"] = urls[params["last"]]
        for key, name in (("aspect", "aspect_ratio"), ("resolution", "resolution"), ("duration", "duration")):
            if params.get(key) is not None:
                args[name] = params[key]
        args["enable_sound"] = bool(params.get("sound"))  # 远端不传时默认开音效，这里一律显式写
    elif kind == "audio":
        tool = "canvas_generate_audio"
        args["mode"] = "audio"
        if params.get("voices"):
            args["ref_audios"] = list(params["voices"])  # 接受媒资 ID，不用换地址
    elif kind == "music":
        tool = "canvas_generate_audio"
        args["mode"] = "music"
        lyrics = params.get("lyrics")
        args["lyrics_mode"] = "ai" if lyrics == "ai" else ("input" if lyrics else "none")
        if lyrics and lyrics != "ai":
            args["lyrics"] = lyrics
    else:
        raise ValueError(kind)
    data = await client.call(tool, args, raw_path=raw_path, retry=False)
    return _task_ids(data)
