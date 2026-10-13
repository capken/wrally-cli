"""提示词拼接：本地只存每镜独有的部分，公共文本由工具拼上。

- 关键帧：独有内容 + style_suffix（按镜头 border 替换边框描述）
- 动画：anim_prefix + 独有内容
- 旁白：narration_template 套上台词（台词里的「」换成『』）

偏离公共文本的镜头在清单里标 `raw: [image, anim]`，文件里存完整文本，原样写回。
"""

from dataclasses import dataclass, field
from typing import Any

from wrally.commands.common import flow_seq
from wrally.errors import PreflightError
from wrally.workspace import Workspace

KINDS = ("image", "anim", "audio")
# 清单里引用提示词文件的字段
FILE_FIELD = {"image": "image_prompt", "anim": "anim_prompt", "audio": "audio_prompt"}
BORDER = "{border}"
SENTENCE_END = ("。", "！", "？")
# `new` 建场次时写入的占位提示词（服务端不接受空场次）；lint 把还没改掉的报成错误
PLACEHOLDER = "（待写）"


@dataclass
class Styles:
    image: str | None = None  # style_suffix
    anim: str | None = None  # anim_prefix
    audio: str | None = None  # narration_template
    borders: dict[str, dict[str, str]] = field(default_factory=dict)
    default_border: str | None = None
    voice_name: str = ""

    def template(self, kind: str) -> str | None:
        return getattr(self, kind)


def load_styles(ws: Workspace, data: dict[str, Any]) -> Styles:
    defaults = ws.settings(data, "defaults")
    styles = Styles(
        borders={str(k): dict(v) for k, v in (defaults.get("borders") or {}).items()},
        default_border=defaults.get("border"),
        voice_name=str((defaults.get("audio") or {}).get("voice_name") or ""),
    )
    for kind, name in (("image", "style_suffix"), ("anim", "anim_prefix"), ("audio", "narration_template")):
        rel = defaults.get(name)
        if rel:
            text = ws.read_text(str(rel))
            if text is None:
                raise PreflightError("style_file_missing", f"({rel})")
            setattr(styles, kind, text.strip("\n"))
    return styles


def raw_kinds(entry: dict[str, Any]) -> list[str]:
    return [str(k) for k in entry.get("raw") or []]


def set_raw(entry: dict[str, Any], kind: str, raw: bool) -> None:
    kinds = [k for k in raw_kinds(entry) if k != kind] + ([kind] if raw else [])
    if kinds:
        entry["raw"] = flow_seq([k for k in KINDS if k in kinds])
    else:
        entry.pop("raw", None)


def is_styled(styles: Styles, entry: dict[str, Any], kind: str) -> bool:
    """这一镜的这类提示词是否由工具拼接公共文本。"""
    return styles.template(kind) is not None and kind not in raw_kinds(entry)


def _fill(styles: Styles, kind: str, border: str | None, key: str) -> str:
    template = styles.template(kind) or ""
    if BORDER not in template:
        return template
    if border not in styles.borders or kind not in styles.borders[border]:
        raise PreflightError("unknown_border", f"({key}: {border})")
    return template.replace(BORDER, styles.borders[border][kind])


def _join(styles: Styles, kind: str, body: str, border: str | None, key: str) -> str:
    if kind == "audio":
        line = body.strip().replace("「", "『").replace("」", "』")
        return (styles.audio or "").replace("{voice_name}", styles.voice_name).replace("{narration}", line)
    common = _fill(styles, kind, border, key)
    if common in body:
        # 文件里已经是完整文本，再拼一次会重复
        raise PreflightError("already_styled", f"({key} {kind}: mark it raw, or pull --force)")
    if kind == "anim":
        return common + body.strip()
    body = body.strip()
    return body + ("" if body.endswith(SENTENCE_END) else "。") + common


def compose(styles: Styles, entry: dict[str, Any], kind: str, body: str) -> str:
    """本地文本 → 发给 WorkRally 的完整提示词。"""
    if not is_styled(styles, entry, kind):
        return body.strip("\n")
    return _join(styles, kind, body, entry.get("border") or styles.default_border, str(entry.get("key")))


def split(styles: Styles, entry: dict[str, Any], kind: str, full: str) -> tuple[str, str | None] | None:
    """完整提示词 → (独有部分, border)。只有拼回去与原文逐字相同才算拆成功，否则返回 None。"""
    template = styles.template(kind)
    if template is None:
        return None
    full = full.strip("\n")
    key = str(entry.get("key"))
    if kind == "audio":
        head, _, tail = template.replace("{voice_name}", styles.voice_name).partition("{narration}")
        if not (full.startswith(head) and full.endswith(tail) and len(full) >= len(head) + len(tail)):
            return None
        body = full[len(head) : len(full) - len(tail)]
        return (body, None) if _join(styles, kind, body, None, key) == full else None
    preferred = entry.get("border") or styles.default_border
    names: list[str | None] = [None]
    if BORDER in template:
        names = [n for n in dict.fromkeys([preferred, *styles.borders]) if n in styles.borders]
    for name in names:
        common = _fill(styles, kind, name, key)
        if kind == "anim" and full.startswith(common):
            body = full[len(common) :]
        elif kind == "image" and full.endswith(common):
            body = full[: -len(common)].removesuffix("。")
        else:
            continue
        if common not in body and _join(styles, kind, body, name, key) == full:
            return body, name
    return None


def local_text(ws: Workspace, entry: dict[str, Any], kind: str) -> str | None:
    """这一镜这类提示词的本地文本：文件正文，或清单里的 narration 台词。"""
    rel = entry.get(FILE_FIELD[kind])
    if rel:
        text = ws.read_text(str(rel))
        return None if text is None else text.strip("\n")
    if kind == "audio" and entry.get("narration"):
        return str(entry["narration"])
    return None


def composed(ws: Workspace, styles: Styles, entry: dict[str, Any], kind: str) -> str | None:
    """本地状态对应的完整提示词；本地没有这类提示词时返回 None。"""
    body = local_text(ws, entry, kind)
    if body is None:
        return None
    if kind == "audio" and entry.get(FILE_FIELD["audio"]):
        return body  # 旁白存成文件时就是完整文本
    return compose(styles, entry, kind, body)
