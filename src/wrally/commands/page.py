"""page：把一集的当前进展做成一个网页 page.html。只读本地文件，不调用远端。

每镜一张卡片：选用的关键帧和候选、视频、旁白、三类提示词，以及每类素材的状态。
图片、视频、音频用相对路径引用工作区里已下载的文件，不内嵌；地址带文件修改时间，换过的素材不会被缓存挡住。

页面内容变了才重写。每次都另写一个 .wrally/page.version.js，开着的页面定时加载它来发现更新
（file:// 下不能 fetch，只能用 script 标签）。
"""

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote

from wrally.commands.common import is_skipped, letter, load_manifest, web_copies
from wrally.errors import WrallyError
from wrally.prompts import KINDS, Styles, composed, is_styled, load_styles, local_text
from wrally.workspace import Workspace, write_if_changed

PAGE = "page.html"
VERSION = "page.version.js"  # 在 .wrally/ 下
SUMMARY = "page.summary.json"  # 在 .wrally/ 下：这一集在总览页上要用的几个数
INDEX = "index.html"  # 项目总览页，和项目文件 wrally.yaml 放在一起
INDEX_VERSION = "index.version.js"  # 在项目目录的 .wrally/ 下
MEDIA = ("keyframe", "video", "audio")
SETTLED = ("done", "skipped")
DATA_MARK, VERSION_MARK = "__WRALLY_DATA__", "__WRALLY_VERSION__"
LABELS = {
    "none": "未开始",
    "pending": "待选图",
    "running": "生成中",
    "done": "完成",
    "failed": "失败",
    "missing": "文件缺失",
    "stale": "输入已变",
    "skipped": "不由 wrally 生成",
}
RUNNING = ("submitting", "running")
WATCHED = ("prompts", "styles", ".wrally/keyframes", ".wrally/video", ".wrally/audio")


@dataclass
class PageReport:
    shots: int
    path: str
    changed: bool
    unit: str = "shots"

    @property
    def summary(self) -> str:
        state = "updated" if self.changed else "unchanged"
        return f"page: {self.shots} {self.unit}  -> {self.path}  ({state})"


def _src(ws: Workspace, path: Path | None) -> str | None:
    """页面里引用本地文件的地址：相对工作区的路径，带修改时间。文件不在时返回 None。"""
    if path is None or not path.is_file():
        return None
    rel = os.path.relpath(path.resolve(), ws.root.resolve())
    return f"{quote(Path(rel).as_posix())}?v={int(path.stat().st_mtime)}"


def _find(directory: Path, stem: str) -> Path | None:
    return next(iter(sorted(directory.glob(f"{stem}.*"))), None)


def _state(state: str, note: str = "") -> dict[str, str]:
    return {"state": state, "label": LABELS[state], "note": note}


def _keyframe(ws: Workspace, entry: dict) -> dict[str, Any]:
    kf = entry.get("keyframe") or {}
    key = str(entry["key"])
    directory = ws.state_dir / "keyframes" / key
    ids = [str(c) for c in kf.get("candidates") or []]
    picked = kf.get("asset_id")
    candidates = [
        {"label": letter(i), "src": _src(ws, _find(directory, letter(i))), "picked": asset_id == picked}
        for i, asset_id in enumerate(ids)
    ]
    src = None
    if picked:
        stem = letter(ids.index(picked)) if picked in ids else f"pick-{picked}"
        src = _src(ws, _find(directory, stem))
    if kf.get("status") in RUNNING:
        status = _state("running")
    elif kf.get("status") == "failed":
        status = _state("failed")
    elif picked:
        status = _state("done") if src else _state("missing", "run: wrally fetch keyframe")
    elif ids:
        status = _state("pending")
    else:
        status = _state("skipped" if is_skipped(entry, "keyframe") else "none")
    pick = letter(ids.index(picked)) if picked in ids else ""
    return {"status": status, "src": src, "pick": pick, "candidates": candidates}


def _media(ws: Workspace, entry: dict, name: str, stale: list[str]) -> dict[str, Any]:
    sec = entry.get(name) or {}
    src = _src(ws, ws.root / str(sec["file"])) if sec.get("file") else None
    state = str(sec.get("status") or "")
    why = next((s for s in stale if s.startswith(name)), "")
    if state in RUNNING:
        status = _state("running")
    elif state == "failed":
        status = _state("failed")
    elif state == "done" and not src:
        status = _state("missing", f"run: wrally fetch {name}")
    elif state == "done":
        status = _state("stale", why) if why else _state("done")
    else:
        status = _state("skipped" if is_skipped(entry, name) else "none")
    return {"status": status, "src": src}


def _prompt(
    ws: Workspace, styles: Styles, entry: dict, kind: str, web_copy: str | None = None
) -> dict[str, Any] | None:
    """一类提示词拆成三段：公共文本（前）、这一镜自己写的、公共文本（后）。

    web_copy 是网页另存的那份副本的文字（在网页上编辑过才有）。有它时生成用的是它，push 改不到它，
    所以页面要标出来：`web` 是 same（两份一致）或 differs（要到网页上粘贴）。
    """
    own = local_text(ws, entry, kind)
    if own is None:
        return None
    raw = not is_styled(styles, entry, kind)
    out = {"before": "", "own": own.strip(), "after": "", "raw": raw, "note": "", "web": None}
    try:
        full = composed(ws, styles, entry, kind) or ""
    except WrallyError as e:
        return out | {
            "note": f"{e.code} {e.hint}".strip(),
            "web": "differs" if web_copy is not None else None,
        }
    if web_copy is not None:
        # 和 Shot.rich_image_differs 同一种比法
        out["web"] = "same" if web_copy.strip("\n") == full.strip("\n") else "differs"
    # 旁白模板会把台词里的「」换成『』，两种写法都找一下
    for body in (out["own"], out["own"].replace("「", "『").replace("」", "』")):
        at = full.find(body) if body else -1
        if at >= 0:
            return out | {"before": full[:at], "own": body, "after": full[at + len(body) :]}
    return out | {"own": full}


def collect(ws: Workspace) -> dict[str, Any]:
    """清单和本地文件 → 页面要展示的数据。"""
    # 放在函数里导入：check 会带进生成命令的模块
    from wrally.commands.check import stale as stale_of

    data = load_manifest(ws)
    note = ""
    try:
        styles = load_styles(ws, data)
    except WrallyError as e:
        styles, note = Styles(), f"{e.code} {e.hint}".strip()
    copies = web_copies(ws) or {}
    shots = []
    for entry in data["shots"]:
        web = copies.get(str(entry.get("story_id"))) or {}
        try:
            stale = stale_of(ws, data, entry)
        except WrallyError:
            stale = []  # 公共文本读不出来，上面已经记了
        audio = _media(ws, entry, "audio", stale)
        f0 = (entry.get("audio") or {}).get("f0")
        shots.append({
            "key": str(entry["key"]),
            "narration": str(entry.get("narration") or ""),
            "keyframe": _keyframe(ws, entry),
            "video": _media(ws, entry, "video", stale),
            "audio": audio | {"f0": f0 if isinstance(f0, (int, float)) else None},
            "prompts": {kind: _prompt(ws, styles, entry, kind, web.get(kind)) for kind in KINDS},
        })  # fmt: skip
    project = ws.project_path()
    return {
        "name": ws.root.resolve().name,
        "title": str(data.get("title") or ""),
        "note": note,
        "film": _src(ws, ws.output_path(data)),
        "shots": shots,
        # 有项目文件时页面上多一个回总览页的入口和各集之间的切换
        "index": f"../{INDEX}" if project and project.parent == ws.root.resolve().parent else None,
        "episodes": [d.name for d in episode_dirs(project.parent)] if project else [],
    }


def episode_dirs(project_dir: Path) -> list[Path]:
    """项目目录下的各集：直接子目录里有 episode.yaml 的。"""
    return sorted(d for d in project_dir.iterdir() if d.is_dir() and (d / "episode.yaml").is_file())


def _web_differs(shot: dict) -> bool:
    return any(p and p["web"] == "differs" for p in shot["prompts"].values())


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    """一集的页面数据 → 总览页上那张卡片要用的数。"""
    shots = data["shots"]
    stats = {}
    for kind in MEDIA:
        states = [s[kind]["status"]["state"] for s in shots]
        stats[kind] = [states.count("done"), len(states) - states.count("skipped")]
    covers = [s["keyframe"]["src"] for s in shots if s["keyframe"]["src"]]
    return {
        "name": data["name"],
        "title": data["title"],
        "shots": len(shots),
        "stats": stats,
        "open": sum(
            _web_differs(s) or any(s[k]["status"]["state"] not in SETTLED for k in MEDIA) for s in shots
        ),
        "web_differs": sum(_web_differs(s) for s in shots),
        # 各集片头常常共用一张图，封面取第二张，各集才分得开
        "cover": covers[1] if len(covers) > 1 else (covers[0] if covers else None),
        "film": bool(data["film"]),
    }


def _template(name: str = "page_template.html") -> str:
    return resources.files("wrally").joinpath(name).read_text(encoding="utf-8")


def render(data: dict[str, Any], template: str = "page_template.html") -> tuple[str, str]:
    """数据 → (网页全文, 版本号)。版本号只由内容决定，所以输入没变时重跑不产生改动。"""
    # 提示词是任意文本：转义掉 < 之后，里面的 </script> 不会把数据块截断
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
    html = _template(template).replace(DATA_MARK, payload)
    version = hashlib.sha256(html.encode("utf-8")).hexdigest()[:12]
    return html.replace(VERSION_MARK, version), version


def _write(page: Path, version_file: Path, html: str, version: str) -> bool:
    changed = write_if_changed(page, html)
    write_if_changed(version_file, f'window.__wrallyPage && window.__wrallyPage("{version}");\n')
    return changed


def build(ws: Workspace, *, index: bool = True) -> PageReport:
    """重建这一集的页面；有项目文件时顺带重建项目总览页。"""
    data = collect(ws)
    html, version = render(data)
    changed = _write(ws.root / PAGE, ws.state_dir / VERSION, html, version)
    summary = json.dumps(summarize(data), ensure_ascii=False, sort_keys=True) + "\n"
    write_if_changed(ws.state_dir / SUMMARY, summary)
    project = ws.project_path()
    if index and project:
        build_index(project.parent)
    return PageReport(shots=len(data["shots"]), path=PAGE, changed=changed)


def _summary(folder: Path) -> dict[str, Any] | None:
    """一集的摘要。还没建过页面的集先建一次；建不出来的（清单是空的、写坏了）不上总览页。"""
    path = folder / ".wrally" / SUMMARY
    if not path.is_file():
        try:
            build(Workspace(folder), index=False)
        except Exception:  # noqa: BLE001
            return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def build_index(project_dir: Path, *, rebuild: bool = False) -> PageReport:
    """项目总览页 index.html：每集一张卡片，点进去是那一集的 page.html。

    只读各集的摘要文件，所以某一集的命令结束后顺带重建它很便宜。rebuild 为真时先把每一集的页面都重建一遍。
    """
    episodes = []
    for folder in episode_dirs(project_dir):
        if rebuild:
            (folder / ".wrally" / SUMMARY).unlink(missing_ok=True)
        summary = _summary(folder)
        if summary is None:
            continue
        cover = f"{quote(folder.name)}/{summary['cover']}" if summary.get("cover") else None
        episodes.append(summary | {"href": f"{quote(folder.name)}/{PAGE}", "cover": cover})
    title = Workspace(project_dir / "_").project().get("title") or project_dir.resolve().name
    html, version = render({"title": str(title), "episodes": episodes}, "index_template.html")
    changed = _write(project_dir / INDEX, project_dir / ".wrally" / INDEX_VERSION, html, version)
    return PageReport(shots=len(episodes), path=INDEX, changed=changed, unit="episodes")


def enabled(ws: Workspace) -> bool:
    """命令结束后要不要顺带重建页面。项目文件或清单里写 `page: false` 就不建；还没有清单时也不建。"""
    data = ws.load()
    if not data or not data.get("shots"):
        return False
    return data.get("page", ws.project().get("page", True)) is not False


def signature(ws: Workspace) -> tuple:
    """页面依赖的所有文件的修改时间；--watch 靠它判断要不要重建。"""
    project = ws.project_path()
    files = [ws.manifest_path, *([project] if project else []), *sorted(ws.root.glob("*.mp4"))]
    folders = [ws.root / name for name in WATCHED] + ([project.parent / "styles"] if project else [])
    for folder in folders:
        files += sorted(p for p in folder.rglob("*") if p.is_file())
    return tuple((str(p), p.stat().st_mtime_ns) for p in files if p.exists())


def watch(
    root: Path,
    *,
    interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    rounds: int | None = None,
) -> int:
    """相关文件一变就重建，直到被中断。返回重建的次数。rounds 只给测试用。"""
    seen, rebuilt = signature(Workspace(root)), 0
    while rounds is None or rounds > 0:
        sleep(interval)
        rounds = None if rounds is None else rounds - 1
        ws = Workspace(root)  # 每轮新建：项目文件读过一次就缓存在 Workspace 里
        now = signature(ws)
        if now == seen:
            continue
        seen = now
        try:
            rebuilt += build(ws).changed
        except Exception:  # noqa: BLE001, S110
            pass  # 文件正改到一半（清单语法不对、提示词文件还没写完），等下一次改动
    return rebuilt
