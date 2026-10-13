"""status：打印全集进度表。只读清单，不调用远端。"""

from pathlib import Path

from wrally.commands.common import letter, load_manifest
from wrally.workspace import Workspace

HEADERS = ("shot", "keyframe", "video", "audio", "f0")


def _keyframe(entry: dict) -> str:
    kf = entry.get("keyframe") or {}
    candidates = list(kf.get("candidates") or [])
    picked = kf.get("asset_id")
    if not picked and not candidates:
        return "-"
    mark = letter(candidates.index(picked)) if picked in candidates else ("set" if picked else "-")
    return f"{mark}/{len(candidates)}"


def _media(ws: Workspace, entry: dict, name: str) -> str:
    sec = entry.get(name) or {}
    state = str(sec.get("status") or "-")
    if state == "done" and not (sec.get("file") and (ws.root / sec["file"]).exists()):
        return "done (no file)"
    return state


def status(ws: Workspace) -> tuple[str, list[tuple[str, ...]]]:
    data = load_manifest(ws)
    rows = []
    for entry in data["shots"]:
        f0 = (entry.get("audio") or {}).get("f0")
        rows.append((str(entry["key"]), _keyframe(entry), _media(ws, entry, "video"),
                     _media(ws, entry, "audio"), str(f0) if f0 else "-"))  # fmt: skip
    picked = sum(1 for e in data["shots"] if (e.get("keyframe") or {}).get("asset_id"))
    video_done = sum(r[2] == "done" for r in rows)
    audio_done = sum(r[3] == "done" for r in rows)
    line = f"{len(rows)} shots: keyframe {picked} picked, video {video_done} done, audio {audio_done} done"
    # 没做完的排前面，表格被截断时先看到它们
    rows.sort(key=lambda r: r[2] == "done" and r[3] == "done")
    return line, rows


PROJECT_HEADERS = ("episode", "shots", "keyframe", "video", "audio", "film")


def project_status(project_dir: Path) -> tuple[str, list[tuple[str, ...]]]:
    """在项目目录（有 wrally.yaml、没有 episode.yaml）下：每集一行，各类素材做完几镜、成片在不在。"""
    from wrally.commands.page import episode_dirs  # 放在函数里导入：page 带进的模块多，单集的 status 用不到

    rows = []
    for folder in episode_dirs(project_dir):
        ws = Workspace(folder)
        data = ws.load() or {}
        shots = list(data.get("shots") or [])
        picked = sum(1 for e in shots if (e.get("keyframe") or {}).get("asset_id"))
        done = [sum(_media(ws, e, name) == "done" for e in shots) for name in ("video", "audio")]
        film = "yes" if ws.output_path(data).is_file() else "-"
        rows.append((folder.name, str(len(shots)), str(picked), str(done[0]), str(done[1]), film))
    films = sum(r[5] == "yes" for r in rows)
    return f"{len(rows)} episodes: {films} with a film  (per-shot table: wrally status -C <episode>)", rows
