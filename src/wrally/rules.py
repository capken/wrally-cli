"""制作规则：check 和 lint 用的阈值与词表。

没有缺省值：每部片子的画风、音色、单镜时长都不一样，阈值和词表写在 wrally.yaml 或 episode.yaml 的
`rules` 小节里。没写的一项就不查；写 null 关掉项目文件里设的某一项。
"""

import re
from dataclasses import dataclass
from typing import Any

from wrally.errors import PreflightError
from wrally.workspace import Workspace, overlay

PUNCT = "，。：？；、“”"  # 数台词字数时不计的标点（书名号、间隔号、『』照常计数）
WORD_KINDS = ("image", "anim")
KEYS = ("f0_max", "slow_max", "narration_max", "channel_gap_max", "words")


def core_len(text: str) -> int:
    """台词的字数，不计标点。"""
    return len(re.sub(f"[{PUNCT}]", "", text))


@dataclass(frozen=True)
class WordRule:
    name: str
    kind: str
    words: tuple[str, ...]


@dataclass(frozen=True)
class Rules:
    f0_max: float | None = None
    slow_max: float | None = None
    narration_max: int | None = None
    channel_gap_max: float | None = None
    words: tuple[WordRule, ...] = ()

    def high_pitch(self, f0: float | None) -> str:
        if f0 is None or self.f0_max is None or f0 <= self.f0_max:
            return ""
        return f"f0 {f0}Hz > {self.f0_max:g}"

    def lopsided(self, gap: float | None) -> str:
        if gap is None or self.channel_gap_max is None or gap <= self.channel_gap_max:
            return ""
        return f"channel gap {gap:g}dB > {self.channel_gap_max:g}"

    def failed_audio(self, audio: dict) -> str:
        """check 写进清单的量值里有没过的：音高超标或左右声道音量差太大。"""
        return self.high_pitch(audio.get("f0")) or self.lopsided(audio.get("channel_gap"))


def _number(name: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PreflightError("bad_rules", f"(rules.{name}: need a positive number or null)")
    return value


def _word_rule(name: str, value: Any) -> WordRule:
    words = value.get("words") if isinstance(value, dict) else None
    if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
        raise PreflightError("bad_rules", f"(rules.words.{name}: need `words`, a list of strings)")
    kind = value.get("kind", "anim")
    if kind not in WORD_KINDS:
        raise PreflightError("bad_rules", f"(rules.words.{name}.kind: use image or anim)")
    return WordRule(name=name, kind=kind, words=tuple(words))


def load_rules(ws: Workspace, data: dict[str, Any]) -> Rules:
    """wrally.yaml ← episode.yaml。写错的键直接报错，免得一条规则悄悄不生效。"""
    merged = overlay({}, ws.settings(data, "rules"))
    unknown = [k for k in merged if k not in KEYS]
    if unknown:
        raise PreflightError("bad_rules", f"(unknown rules.{unknown[0]})")
    groups = merged.get("words") or {}
    if not isinstance(groups, dict):
        raise PreflightError("bad_rules", "(rules.words: need a mapping of name to {kind, words})")
    return Rules(
        f0_max=_number("f0_max", merged.get("f0_max")),
        slow_max=_number("slow_max", merged.get("slow_max")),
        narration_max=_number("narration_max", merged.get("narration_max")),
        channel_gap_max=_number("channel_gap_max", merged.get("channel_gap_max")),
        words=tuple(_word_rule(str(k), v) for k, v in groups.items() if v is not None),
    )
