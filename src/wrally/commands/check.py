"""check：生成检查材料。只读本地文件，不调用远端。

每镜一张抽帧拼图（小尺寸）、旁白时长与音高、放慢倍数、台词字数；明细写 .wrally/report.md，
终端只列有警告的镜头。查哪几项、阈值多少取自 rules；没设 f0_max 就不量音高。
"""

from dataclasses import dataclass, field

from wrally import media
from wrally.commands.common import load_manifest, section, select
from wrally.errors import WrallyError
from wrally.rules import core_len, load_rules
from wrally.workspace import Workspace, write_if_changed

HEADERS = ("shot", "video", "audio", "slow", "f0", "warnings")


@dataclass
class CheckReport:
    rows: list[tuple[str, ...]] = field(default_factory=list)
    sheets: int = 0
    warnings: int = 0
    report_path: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        counts = f"{self.sheets} sheets, {self.warnings} warnings"
        return f"checked {len(self.rows)} shots: {counts}  -> {self.report_path}"

    def flagged(self) -> list[tuple[str, ...]]:
        return [r for r in self.rows if r[5]]


def _limit(value: float | None) -> str:
    return "不限" if value is None else f"{value:g}"


def _file(ws: Workspace, entry: dict, name: str):
    rel = (entry.get(name) or {}).get("file")
    path = ws.root / rel if rel else None
    return path if path and path.exists() else None


def stale(ws: Workspace, data: dict, entry: dict) -> list[str]:
    """素材是不是用现在的输入做的。只对 wrally 生成的素材有效：它们在清单里留有输入指纹 made_from。

    台词改了而旁白没重做，字幕和声音就会对不上；选图或动画提示词改了而视频没重做同理。
    """
    # 放在函数里导入：生成命令的模块很重，check 的其余部分用不到
    from wrally.commands import audio as audio_cmd
    from wrally.commands import video as video_cmd
    from wrally.prompts import composed, load_styles

    out = []
    styles = load_styles(ws, data)
    defaults = ws.settings(data, "defaults")
    try:
        audio = entry.get("audio") or {}
        if audio.get("made_from"):
            voice = defaults.get("audio") or {}
            text = composed(ws, styles, entry, "audio") or ""
            if (
                audio_cmd.fingerprint(text, voice.get("model"), voice.get("voice_asset"))
                != audio["made_from"]
            ):
                out.append("audio was made from different narration")
        video = entry.get("video") or {}
        pick = (entry.get("keyframe") or {}).get("asset_id")
        if video.get("made_from") and pick:
            prompt = composed(ws, styles, entry, "anim") or ""
            settings = video_cmd.shot_settings(video_cmd.video_settings(defaults), entry)
            if video_cmd.fingerprint(pick, prompt, settings) != video["made_from"]:
                out.append("video was made from a different keyframe or prompt")
    except WrallyError:
        pass  # 提示词拼不出来由 lint 报，这里不重复
    return out


def check(ws: Workspace, spec: str = "all") -> CheckReport:
    data = load_manifest(ws)
    rules = load_rules(ws, data)
    report = CheckReport(report_path=(ws.state_dir / "report.md").relative_to(ws.root).as_posix())
    lines = []
    # 量音高要用 librosa，它是可选依赖：没装就跳过这一项并说明，其余照常检查
    pitch = rules.f0_max is not None and media.pitch_available()
    if rules.f0_max is not None and not pitch:
        report.notes.append(f"note: pitch not checked, librosa is not installed ({media.PITCH_HINT})")
    for entry in select(data, spec):
        key = str(entry["key"])
        warnings: list[str] = []
        video, audio = _file(ws, entry, "video"), _file(ws, entry, "audio")
        vlen = alen = 0.0
        sheet = ""
        if video:
            vlen = media.duration(video)
            out = ws.state_dir / "sheets" / f"{key}.jpg"
            # 视频没换就不重新抽帧
            if (out.exists() and out.stat().st_mtime >= video.stat().st_mtime) or media.contact_sheet(
                video, out
            ):
                sheet = out.relative_to(ws.root).as_posix()
                report.sheets += 1
            else:
                warnings.append("sheet failed")
        f0 = None
        if audio:
            alen = media.duration(audio)
            sec = section(entry, "audio")
            f0 = sec.get("f0")
            if f0 is None and pitch:
                measured = media.median_f0(audio)
                if measured is not None:
                    f0 = sec["f0"] = round(measured)
            if why := rules.high_pitch(f0):
                warnings.append(why)
        slow = alen / vlen if vlen and alen else 0.0
        if rules.slow_max is not None and slow > rules.slow_max:
            warnings.append(f"slow x{slow:.2f} > {rules.slow_max:g}")
        warnings += stale(ws, data, entry)
        narration = str(entry.get("narration") or "")
        if rules.narration_max is not None and core_len(narration) > rules.narration_max:
            warnings.append(f"narration {core_len(narration)} chars > {rules.narration_max}")
        report.warnings += len(warnings)
        row = (
            key,
            f"{vlen:.1f}s" if vlen else "-",
            f"{alen:.1f}s" if alen else "-",
            f"x{slow:.2f}" if slow else "-",
            str(f0) if f0 is not None else "-",
            "; ".join(warnings),
        )
        report.rows.append(row)
        lines.append("| " + " | ".join([*row[:5], sheet or "-", row[5] or "-"]) + " |")
    ws.save(data)
    text = "\n".join([
        "# 检查报告", "",
        f"{len(report.rows)} 镜，{report.warnings} 条警告。"
        f"阈值：音高 ≤ {_limit(rules.f0_max)} Hz，放慢倍数 ≤ {_limit(rules.slow_max)}，"
        f"旁白 ≤ {_limit(rules.narration_max)} 字。", "",
        "| 镜号 | 视频 | 旁白 | 放慢 | 音高 Hz | 抽帧图 | 警告 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *lines, "",
        *[line for note in report.notes for line in (note, "")],
    ])  # fmt: skip
    write_if_changed(ws.state_dir / "report.md", text)
    return report
