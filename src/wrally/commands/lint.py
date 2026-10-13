"""lint：检查本地提示词和台词里已知会出问题的写法。只读本地，不调用远端。

警告不影响退出码；提示词拼不出来（缺文件、已含公共文本、未知边框）
或还是 `new` 留下的占位文本，是错误，退出码 2。
"""

import re
from dataclasses import dataclass, field

from wrally.commands.common import load_manifest, select
from wrally.errors import PreflightError
from wrally.prompts import FILE_FIELD, PLACEHOLDER, compose, load_styles, local_text
from wrally.rules import core_len, load_rules
from wrally.workspace import Workspace, write_if_changed

HEADERS = ("shot", "level", "problem")
NEGATIONS = ("不出现", "不要", "没有", "不使用", "不能", "避免", "禁止")


@dataclass
class LintReport:
    rows: list[tuple[str, ...]] = field(default_factory=list)
    shots: int = 0

    @property
    def errors(self) -> int:
        return sum(r[1] == "error" for r in self.rows)

    @property
    def warnings(self) -> int:
        return sum(r[1] == "warn" for r in self.rows)

    @property
    def summary(self) -> str:
        return (
            f"linted {self.shots} shots: {self.warnings} warnings, {self.errors} errors  -> .wrally/lint.md"
        )


def _negated(text: str, start: int) -> bool:
    """命中的词是不是在否定句里。

    两种写法都算：整句用「不出现 A、B、C」统领一串词；或者紧挨着写「不碰杯」「卷轴不展开」。
    """
    sentence = re.split(r"[。；！？\n]", text[:start])[-1]
    if any(n in sentence for n in NEGATIONS):
        return True
    phrase = re.split(r"[，、：]", sentence)[-1]
    return any(n in phrase for n in "不没无勿别")


def find_words(text: str, words: tuple[str, ...]) -> list[str]:
    """找出文本里以肯定语气出现的词。"""
    return [w for w in words if any(not _negated(text, m.start()) for m in re.finditer(re.escape(w), text))]


def lint(ws: Workspace, spec: str = "all") -> LintReport:
    data = load_manifest(ws)
    styles = load_styles(ws, data)
    rules = load_rules(ws, data)
    report = LintReport()
    for entry in select(data, spec):
        key = str(entry["key"])
        report.shots += 1

        def add(level: str, problem: str, key: str = key) -> None:
            report.rows.append((key, level, problem))

        for kind in ("image", "anim"):
            ref = entry.get(FILE_FIELD[kind])
            body = local_text(ws, entry, kind)
            if body is None:
                add(
                    "error" if ref else "warn",
                    f"{kind}: {'file missing ' + str(ref) if ref else 'no prompt'}",
                )
                continue
            if not body.strip():
                add("warn", f"{kind}: empty prompt")
                continue
            if body.strip() == PLACEHOLDER:
                add("error", f"{kind}: placeholder not replaced")
                continue
            try:
                compose(styles, entry, kind, body)
            except PreflightError as e:
                add("error", f"{kind}: {e.code}")
            refs = (entry.get("keyframe") or {}).get("refs") or []
            if kind == "image" and refs and not re.search(r"第[一二三四五六七八九十\d]+张图", body):
                # 画布靠「第一张图片」「第二张图片」把提示词和参考图对上
                add("warn", f"image: has {len(refs)} refs but prompt never says 第N张图片")
            # 只查这一镜自己写的部分，公共前缀里的「不出现光束」不算
            for rule in rules.words:
                if rule.kind == kind and (hit := find_words(body, rule.words)):
                    add("warn", f"{kind}: {rule.name} words {'、'.join(hit)}")

        narration = str(entry.get("narration") or "")
        if narration:
            if rules.narration_max is not None and core_len(narration) > rules.narration_max:
                add("warn", f"narration: {core_len(narration)} chars > {rules.narration_max}")
        elif not entry.get(FILE_FIELD["audio"]):
            add("warn", "narration: missing")

    report.rows.sort(key=lambda r: r[1] != "error")
    lines = [
        "# lint",
        "",
        report.summary.split("  ->")[0],
        "",
        "| 镜号 | 级别 | 问题 |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} |" for r in report.rows]
    write_if_changed(ws.state_dir / "lint.md", "\n".join(lines) + "\n")
    return report
