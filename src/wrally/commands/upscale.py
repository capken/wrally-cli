"""video upscale：给每镜现在用的视频（清单里的 video.asset_id）做超分。

走画布接口 canvas_upscale_video：它收媒资 ID，超分的就是清单里那一条，不受网页上「选定视频」影响。
模型和倍数每次从 canvas_upscale_config 取；选项 → defaults.upscale → 远端默认值。

状态记在每镜的 video.upscaled 里：
- made_from 是输入指纹（源视频 asset_id + 模型 + 倍数），没变就不重复提交。
- 提交前先落盘 submitting，受理后写任务 ID；提交超时时停在 submitting，再跑报 pending_submit，
  要确认远端没受理后用 --redo，避免重复扣额度。
- 等待和下载可以中断，重跑同一条命令接着做。
- source 记的是源视频；视频换了（fetch video 拿到新的一条），超分版就算过期，export 改用原视频。
"""

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import httpx2

from wrally.api import canvas
from wrally.api.assets import download_assets
from wrally.api.image import canvas_task
from wrally.commands.common import is_skipped, load_manifest, section, select
from wrally.errors import AuthError, NetworkError, PreflightError, WrallyError
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("shot", "status", "detail")
INITIAL = 60  # 首次轮询前的等待（秒）
INTERVAL = 15


@dataclass
class Report:
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    failed: int = 0
    timed_out: int = 0
    dry_run: bool = False
    label: str = ""  # 模型名称和倍数，如 UpScale-02-v2 x2
    elapsed: float = 0  # collect 等了多久

    @property
    def summary(self) -> str:
        skipped = sum(r[1] == "skipped" for r in self.rows)
        tail = f", {skipped} skipped" if skipped else ""
        tail += f", {self.failed} failed" if self.failed else ""
        if self.dry_run:
            would = " ".join(r[0] for r in self.rows if r[1] == "would submit")
            count = sum(r[1] == "would submit" for r in self.rows)
            return f"dry run: would upscale {count} videos with {self.label}  ({would or '-'}){tail}"
        keys = " ".join(self.submitted) or "-"
        return f"submitted {len(self.submitted)} upscale tasks with {self.label}  ({keys}){tail}"

    @property
    def waited(self) -> str:
        done = sum(r[1] == "done" for r in self.rows)
        tail = f", {self.timed_out} still running (timeout)" if self.timed_out else ""
        return f"waited {self.elapsed:.0f}s: {done} upscaled, {self.failed} failed{tail}"


def fingerprint(source: str, model: str, scale: str) -> str:
    payload = json.dumps([source, model, scale], ensure_ascii=False)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def current_file(ws: Workspace, entry: dict) -> Path | None:
    """这一镜可用的超分版：做完了、文件在、而且是从现在这条视频做的。"""
    video = entry.get("video") or {}
    up = video.get("upscaled") or {}
    if up.get("status") != "done" or not up.get("file") or up.get("source") != video.get("asset_id"):
        return None
    path = ws.root / str(up["file"])
    return path if path.is_file() else None


def is_stale(entry: dict) -> bool:
    """做过超分，但视频后来换了一条。"""
    video = entry.get("video") or {}
    up = video.get("upscaled") or {}
    return up.get("status") == "done" and bool(up.get("source")) and up.get("source") != video.get("asset_id")


def resolve(options: canvas.UpscaleOptions, model: str | None, scale: str | None) -> tuple[str, str]:
    """模型（ID 或名称，不分大小写）和倍数，校验后返回 (模型 ID, 倍数)。"""
    wanted = model or options.default_model
    if not wanted:
        raise PreflightError("missing_model", "(no default upscale model on the server; use --model)")
    found = next((i for i, name in options.models if wanted == i or wanted.lower() == name.lower()), None)
    if found is None:
        names = ", ".join(name for _, name in options.models)
        raise PreflightError("unknown_model", f"(upscale: {wanted}; allowed: {names})")
    scale = str(scale) if scale is not None else options.default_scale
    if not scale or (options.scales and scale not in options.scales):
        raise PreflightError("bad_scale", f"({scale}; allowed: {', '.join(options.scales)})")
    return found, scale


def _keys(entries: list[dict]) -> str:
    return ",".join(str(e["key"]) for e in entries)


async def upscale(
    ws: Workspace,
    client: McpClient,
    spec: str = "all",
    *,
    model: str | None = None,
    scale: str | None = None,
    redo: bool = False,
    dry_run: bool = False,
) -> Report:
    """分类 → 落盘 submitting → 逐镜提交。等待和下载在 collect 里。"""
    data = load_manifest(ws)
    entries = select(data, spec)
    settings = ws.settings(data, "defaults").get("upscale") or {}
    options = await canvas.upscale_options(client, ws.raw_dir / "upscale-config.json")
    model_id, scale = resolve(options, model or settings.get("model"), scale or settings.get("scale"))
    report = Report(dry_run=dry_run, label=f"{options.model_name(model_id)} x{scale}")

    no_video = []
    todo = []
    for e in entries:
        key, video = str(e["key"]), e.get("video") or {}
        source = video.get("asset_id")
        if not source:
            if is_skipped(e, "video"):
                report.rows.append((key, "skipped", "marked skip in episode.yaml"))
            else:
                no_video.append(e)
            continue
        if video.get("status") in ("submitting", "running"):
            report.rows.append((key, "skipped", "a new video is still being made"))
            continue
        up = video.get("upscaled") or {}
        same = up.get("made_from") == fingerprint(str(source), model_id, scale)
        if redo:
            todo.append(e)
        elif same and up.get("status") == "submitting":
            report.failed += 1
            report.rows.append(
                (key, "error: pending_submit", "may have been accepted; check WorkRally, then --redo")
            )
        elif same and up.get("status") in ("running", "done"):
            report.rows.append((key, "skipped", f"{up['status']}, inputs unchanged"))
        else:
            todo.append(e)
    if no_video:
        raise PreflightError("missing_video", f"({_keys(no_video)}; run: wrally fetch video)")
    if dry_run:
        report.rows += [(str(e["key"]), "would submit", "") for e in todo]
        return report

    # 先全部落盘 submitting，再逐镜提交，每镜受理后立刻写回
    for e in todo:
        video = section(e, "video")
        source = str(video["asset_id"])
        video["upscaled"] = {
            "made_from": fingerprint(source, model_id, scale),
            "source": source,
            "model": model_id,
            "scale": scale,
            "status": "submitting",
        }
    if todo:
        ws.save(data)
    title = str(data.get("title") or ws.root.resolve().name)
    for e in todo:
        key, up = str(e["key"]), e["video"]["upscaled"]
        raw = ws.raw_dir / f"{key}-upscale-submit.json"
        try:
            tasks = await canvas.submit_upscale(
                client, up["source"], model=model_id, scale=scale, project_id=data.get("project_id"),
                name=f"{title} {key} x{scale}", raw_path=raw,
            )  # fmt: skip
        except AuthError:
            raise
        except NetworkError as err:
            report.failed += 1  # 不知道服务端是否已受理：停在 submitting
            report.rows.append((key, f"error: {err.code}", "may have been accepted; check WorkRally"))
            continue
        except WrallyError as err:
            up["status"] = "failed"  # 服务端明确拒绝，没有扣额度
            report.failed += 1
            report.rows.append((key, f"error: {err.code}", f"(raw: {raw})"))
            ws.save(data)
            continue
        if not tasks:
            up["status"] = "failed"  # 读不到源片的宽高时长时服务端不提交这一条
            report.failed += 1
            report.rows.append((key, "error: not_submitted", f"(raw: {raw})"))
        else:
            up["status"] = "running"
            up["task"] = tasks[0]
            up["submitted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            report.submitted.append(key)
            report.rows.append((key, "submitted", tasks[0]))
        ws.save(data)
    report.rows.sort(key=lambda r: not r[1].startswith("error"))
    return report


async def collect(
    ws: Workspace,
    client: McpClient,
    spec: str = "all",
    *,
    timeout: float = 1200,
    http: httpx2.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    """等超分任务结束并下载。只看所选镜头里在跑的、做完还没下载的。"""
    data = load_manifest(ws)
    entries = [e for e in select(data, spec) if (e.get("video") or {}).get("upscaled")]
    report = Report()
    started = clock()

    def ups(state: str) -> list[tuple[dict, dict]]:
        return [(e, e["video"]["upscaled"]) for e in entries if e["video"]["upscaled"].get("status") == state]

    async def poll() -> None:
        for e, up in ups("running"):
            if not up.get("task"):
                continue
            raw = ws.raw_dir / f"{e['key']}-upscale-task.json"
            got = await canvas_task(client, str(up["task"]), data.get("project_id"), raw_path=raw)
            if got.state == "done" and got.asset_ids:
                up["status"], up["asset_id"] = "done", got.asset_ids[0]
                up.pop("error", None)
            elif got.state != "running":
                up["status"] = "failed"
                up["error"] = got.error or "no_asset"

    await poll()
    if ups("running"):
        await sleep(min(INITIAL, max(0.0, timeout - (clock() - started))))
        while True:
            await poll()
            if not ups("running") or clock() - started >= timeout:
                break
            await sleep(min(INTERVAL, max(0.0, timeout - (clock() - started))))
    ws.save(data)

    def downloaded(up: dict) -> bool:
        return bool(up.get("file")) and (ws.root / str(up["file"])).is_file()

    want = [(e, up) for e, up in ups("done") if up.get("asset_id") and not downloaded(up)]
    items = [(str(up["asset_id"]), ws.state_dir / "video" / f"{e['key']}-x{up['scale']}") for e, up in want]
    downloads = await download_assets(client, items, raw_path=ws.raw_dir / "upscale-assets.json", http=http)
    errors = {}
    for (e, up), d in zip(want, downloads, strict=True):
        if d.ok and d.path:
            up["file"] = d.path.relative_to(ws.root).as_posix()
        else:
            errors[str(e["key"])] = d.error or "download"
    ws.save(data)
    report.elapsed = clock() - started

    for e in entries:
        key, up = str(e["key"]), e["video"]["upscaled"]
        state = up.get("status")
        if state == "running":
            report.timed_out += 1
            report.rows.append((key, "running", str(up.get("task") or "")))
        elif state == "failed":
            report.failed += 1
            report.rows.append((key, "failed", str(up.get("error") or "")))
        elif state == "done" and key in errors:
            report.failed += 1
            report.rows.append((key, f"error: {errors[key]}", ""))
        elif state == "done":
            note = "  (video changed since; stale)" if is_stale(e) else ""
            report.rows.append((key, "done", f"{up.get('file') or ''}{note}"))
    report.rows.sort(key=lambda r: r[1] == "done")
    return report
