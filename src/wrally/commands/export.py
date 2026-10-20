"""export：把合成需要的输入写成一份中性的 JSON（.wrally/export.json）。只读本地文件，不调用远端。

wrally 不做合成，也不认识任何一部片子的版式。合成脚本只读这一个文件：每镜用哪个视频、哪段旁白、
台词是什么；清单和项目文件里 wrally 不认识的键原样放在 extra 里，含义由脚本自己定。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wrally import media
from wrally.commands.check import stale
from wrally.commands.common import load_manifest, select
from wrally.workspace import Workspace, overlay, plain, write_if_changed

HEADERS = ("shot", "missing", "stale")
EXPORT = "export.json"  # 在 .wrally/ 下
VERSION = 1
# wrally 自己用的键；其余的都算 extra
TOP_KEYS = ("project_id", "series_id", "title", "defaults", "rules", "shots", "page", "output")
SHOT_KEYS = (
    "key", "story_id", "skip", "narration", "border", "raw", "video_duration", "video_resolution",
    "image_prompt", "anim_prompt", "audio_prompt", "keyframe", "video", "audio",
)  # fmt: skip


@dataclass
class ExportReport:
    shots: int = 0
    path: str = ""
    changed: bool = False
    rows: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        state = "updated" if self.changed else "unchanged"
        flagged = f", {len(self.rows)} with missing or stale material" if self.rows else ""
        return f"exported {self.shots} shots{flagged}  -> {self.path}  ({state})"


def _rel(ws: Workspace, path: Path) -> str:
    """工作区内的写相对路径，这样整个目录搬走后导出的内容不变。"""
    try:
        return path.resolve().relative_to(ws.root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _material(ws: Workspace, entry: dict, name: str) -> tuple[str | None, float | None]:
    """（清单里记的文件，时长）。文件不在时时长是 None。"""
    rel = (entry.get(name) or {}).get("file")
    if not rel:
        return None, None
    path = ws.root / str(rel)
    return _rel(ws, path), round(media.duration(path), 3) if path.is_file() else None


def collect(ws: Workspace, data: dict, spec: str = "all") -> dict[str, Any]:
    shots = []
    for entry in select(data, spec):
        video, video_seconds = _material(ws, entry, "video")
        audio, audio_seconds = _material(ws, entry, "audio")
        narration = str(entry.get("narration") or "")
        missing = [
            name
            for name, there in (
                ("video", video_seconds is not None),
                ("audio", audio_seconds is not None),
                ("narration", bool(narration.strip())),
            )
            if not there
        ]
        shots.append({
            "key": str(entry["key"]),
            "video": video,
            "video_seconds": video_seconds,
            "audio": audio,
            "audio_seconds": audio_seconds,
            "narration": narration,
            "skip": [str(k) for k in entry.get("skip") or []],
            "missing": missing,
            "stale": stale(ws, data, entry),
            "extra": {str(k): plain(v) for k, v in entry.items() if k not in SHOT_KEYS},
        })  # fmt: skip
    project, shared = ws.project_path(), ws.project()
    extra = {str(k): plain(v) for k, v in shared.items() if k not in TOP_KEYS}
    for name, value in data.items():
        if name not in TOP_KEYS:  # 和 ws.settings 同一种合并：项目文件打底，单集清单覆盖
            extra[str(name)] = overlay(extra.get(str(name)), value)
    return {
        "version": VERSION,
        "title": str(data.get("title") or ""),
        "output": _rel(ws, ws.output_path(data)),
        # extra 里取自项目文件的相对路径是相对这个目录的；wrally 不知道 extra 里哪些值是路径，由读的一方解析
        "project_dir": os.path.relpath(project.parent, ws.root.resolve()) if project else None,
        "shots": shots,
        "extra": extra,
    }


def export(ws: Workspace, spec: str = "all") -> ExportReport:
    data = load_manifest(ws)
    content = collect(ws, data, spec)
    path = ws.state_dir / EXPORT
    report = ExportReport(shots=len(content["shots"]), path=path.relative_to(ws.root).as_posix())
    report.changed = write_if_changed(path, json.dumps(content, ensure_ascii=False, indent=2) + "\n")
    for shot in content["shots"]:
        if shot["missing"] or shot["stale"]:
            missing, old = ",".join(shot["missing"]), "; ".join(shot["stale"])
            report.rows.append((shot["key"], missing or "-", old or "-"))
    return report
