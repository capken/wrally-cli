"""WorkRally 返回结构的类型化模型。模型里不保留任何 URL。"""

import json
from typing import Any

from pydantic import BaseModel, Field

# 远端 story_num 里的非数字镜号 → 清单里的 key
SPECIAL_KEYS = {"片头": "00", "片尾": "99"}


def shot_key(story_num: str) -> str:
    """把远端的 story_num 映射成清单里的镜号：片头 → 00，片尾 → 99，数字补成两位。"""
    num = story_num.strip()
    if num in SPECIAL_KEYS:
        return SPECIAL_KEYS[num]
    return num.zfill(2) if num.isdigit() else num


def _loads(value: Any) -> Any:
    """extra、gen_config 等字段是嵌套的 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value) if value else None
        except ValueError:
            return None
    return value


def strip_urls(value: Any) -> Any:
    """递归去掉所有 URL 字符串，保证签名地址不会离开接口层。"""
    if isinstance(value, dict):
        return {
            k: strip_urls(v)
            for k, v in value.items()
            if not (isinstance(v, str) and v.startswith(("http://", "https://")))
        }
    if isinstance(value, list):
        return [strip_urls(v) for v in value]
    return value


def rich_text(value: Any) -> str | None:
    """网页编辑器存的富文本提示词（段落 → 文本节点）转成纯文本；字段不存在或没有文字时返回 None。"""
    doc = _loads(value)
    if not isinstance(doc, dict):
        return None
    paragraphs = (doc.get("root") or {}).get("children") or []
    lines = [
        "".join(str(node.get("text") or "") for node in p.get("children") or [] if isinstance(node, dict))
        for p in paragraphs
        if isinstance(p, dict)
    ]
    text = "\n".join(lines)
    return text if text.strip() else None


class Shot(BaseModel):
    story_id: str
    story_num: str
    sort_num: int = 0
    image_prompt: str = ""
    # 网页编辑器另存的一份图片提示词（extra.image_prompt_json）。在网页上编辑过才有；
    # 有它时场次生图用的是它，而 edit_content 改不到它
    image_prompt_rich: str | None = None
    animation_prompt: str = ""
    # 动画提示词同样有一份网页副本（extra.animation_prompt_json），场次生视频用的是它
    animation_prompt_rich: str | None = None
    audio_prompt: str = ""
    gen_config: dict[str, Any] = Field(default_factory=dict)
    keyframe_asset_id: str | None = None  # 网页「选用」的关键帧：gen_config.video.singleAssets
    video_asset_id: str | None = None  # 网页选用的视频
    voice_asset_ids: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return shot_key(self.story_num)

    def rich_image_differs(self, text: str) -> bool:
        """网页那份图片提示词存在且与 text 不同：这时场次生图不会用 text。"""
        return self.image_prompt_rich is not None and self.image_prompt_rich.strip("\n") != text.strip("\n")

    def rich_anim_differs(self, text: str) -> bool:
        """网页那份动画提示词存在且与 text 不同：这时场次生视频不会用 text。"""
        rich = self.animation_prompt_rich
        return rich is not None and rich.strip("\n") != text.strip("\n")

    @classmethod
    def from_story(cls, story: dict[str, Any]) -> "Shot":
        """解析 shotlist_manage list 返回的一条 story。"""
        extra = _loads(story.get("extra")) or {}
        gen_config = strip_urls(_loads(extra.get("gen_config")) or {})
        singles = (gen_config.get("video") or {}).get("singleAssets") or []
        keyframe = next((a.get("asset_id") or a.get("assetId") for a in singles if isinstance(a, dict)), None)
        voices = _loads(extra.get("audio_role_data_json")) or []
        selected = story.get("story_audit_asset_detail") or {}
        return cls(
            story_id=story["story_id"],
            story_num=str(story.get("story_num") or ""),
            sort_num=int(story.get("sort_num") or 0),
            image_prompt=story.get("image_prompt") or "",
            image_prompt_rich=rich_text(extra.get("image_prompt_json")),
            animation_prompt=story.get("animation_prompt") or "",
            animation_prompt_rich=rich_text(extra.get("animation_prompt_json")),
            audio_prompt=extra.get("audio_prompt") or "",
            gen_config=gen_config,
            keyframe_asset_id=keyframe or None,
            video_asset_id=selected.get("asset_id") or None,
            voice_asset_ids=[v["asset_id"] for v in voices if isinstance(v, dict) and v.get("asset_id")],
        )
