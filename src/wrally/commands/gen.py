"""wrally gen：不建项目和清单，单独生成图片、视频、音频、音乐，结果直接下载成文件。

状态记在输出目录的 .wrally-gen.json 里（每次提交一条记录），不依赖对话记忆：
- 提交前先落盘为 submitting，受理后写入任务 ID。提交超时时记录停在 submitting，
  同样的参数再跑会被拦下（pending_submit），避免重复扣额度。
- 同样的参数已经提交过就跳过；要再出一批用 --again。
- 等待和下载可以中断，`wrally gen fetch -o <目录>` 接着做。
"""

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anyio
import httpx2

from wrally.api import canvas
from wrally.api.assets import download_assets
from wrally.api.image import canvas_task
from wrally.errors import NetworkError, PreflightError, WrallyError
from wrally.transport.client import McpClient

LOG = ".wrally-gen.json"
RAW = ".wrally-gen-raw"
POLL_SECONDS = 3.0
HEADERS = ("job", "state", "file")


@dataclass
class Report:
    summary: str = ""
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    failed: int = 0
    timed_out: int = 0
    job_ids: list[str] = field(default_factory=list)  # 本次提交或接上的记录


# ---------- 提交记录 ----------


def load_log(out: Path) -> list[dict]:
    path = out / LOG
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("jobs") or []


def save_log(out: Path, jobs: list[dict]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    path = out / LOG
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "jobs": jobs}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)  # 原子替换：中断不会留下半份记录


def fingerprint(kind: str, params: dict, project_id: str | None) -> str:
    """参数指纹：kind、模型 ID 和全部生成参数（不含文件名）。"""
    body = {k: v for k, v in params.items() if k != "name"} | {"kind": kind, "project": project_id}
    return hashlib.sha1(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]


def _base(job: dict) -> str:
    return job["params"].get("name") or f"{job['kind']}-{job['fp'][:6]}"


def _files_for(job: dict) -> list[str]:
    """每个任务的文件名（不含扩展名，下载时按媒资补上）。同一文件名的后续批次加 -b2、-b3 区分。"""
    base = _base(job) + (f"-b{job['batch']}" if job.get("batch", 1) > 1 else "")
    n = len(job["tasks"])
    return [base if n == 1 else f"{base}-{i + 1}" for i in range(n)]


# ---------- 校验 ----------


def validate(kind: str, model: canvas.Model, params: dict) -> None:
    def bad(what: str, value: object, allowed: list) -> None:
        shown = ", ".join(str(a) for a in allowed)
        raise PreflightError(f"bad_{what}", f"({model.name}: {value}; allowed: {shown})")

    count = params.get("count", 1)
    if not 1 <= count <= 4:
        bad("count", count, [1, 2, 3, 4])
    if params.get("aspect") and model.aspects and params["aspect"] not in model.aspects:
        bad("aspect", params["aspect"], model.aspects)
    res = params.get("resolution")
    if res is not None and model.resolutions and str(res) not in model.resolutions:
        bad("resolution", res, model.resolutions)
    if kind == "video":
        mode = params.get("mode", "text")
        if mode not in model.modes:
            bad("mode", mode, model.modes)
        if mode == "firstlast" and not (params.get("image") or params.get("last")):
            raise PreflightError("missing_frame", "(firstlast needs --image and/or --last)")
        if (
            params.get("duration") is not None
            and model.durations
            and params["duration"] not in model.durations
        ):
            bad("duration", params["duration"], model.durations)
        if params.get("sound") and not model.sound:
            raise PreflightError("no_sound", f"({model.name} cannot generate sound)")
    if kind == "audio" and params.get("voices"):
        if model.minimax:
            raise PreflightError("no_ref_audio", f"({model.name} does not take reference audio)")
        if model.max_refs and len(params["voices"]) > model.max_refs:
            bad("voice_count", len(params["voices"]), list(range(1, model.max_refs + 1)))


# ---------- 提交 ----------


async def gen(
    client: McpClient,
    kind: str,
    params: dict,
    out: Path,
    *,
    project_id: str | None = None,
    again: bool = False,
    dry_run: bool = False,
    http: httpx2.AsyncClient | None = None,
) -> Report:
    """校验参数并提交一次生成。params 里的 model 可以是 ID 或名称，这里换成 ID。"""
    if not (params.get("prompt") or "").strip():
        raise PreflightError("missing_prompt", "(give PROMPT or --prompt-file)")
    model = canvas.pick_model(await canvas.models(client, kind), params["model"], kind)
    params = params | {"model": model.id}
    validate(kind, model, params)
    fp = fingerprint(kind, params, project_id)
    jobs = load_log(out)
    same = [j for j in jobs if j["fp"] == fp]
    report = Report()
    if same and not again:
        last = same[-1]
        if last["state"] == "submitting":
            raise PreflightError(
                "pending_submit",
                f"(job {last['id']} may have been accepted before a timeout; check WorkRally, then --again)",
            )
        if last["state"] != "failed":
            report.summary = (
                f"{kind}: same inputs already submitted as job {last['id']}, skipped (--again for more)"
            )
            report.job_ids = [last["id"]]
            return report
    if dry_run:
        report.summary = (
            f"dry run: would submit 1 {kind} job, {params.get('count', 1)} result(s) with {model.name}"
        )
        return report
    # 先换好参考图地址再落盘 submitting：这一步失败时什么都没提交，不该留下「可能已受理」的记录
    request = await canvas.prepare(client, kind, params, project_id=project_id, http=http)
    job = {
        "id": f"{len(jobs) + 1:03d}",
        "fp": fp,
        # 同一个文件名下的第几批：换了参数但沿用 --name 时也不覆盖已有的文件
        "batch": sum(1 for j in jobs if _base(j) == (params.get("name") or f"{kind}-{fp[:6]}")) + 1,
        "kind": kind,
        "model_name": model.name,
        "project_id": project_id,
        "params": params,
        "state": "submitting",
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
        "tasks": [],
    }
    jobs.append(job)
    save_log(out, jobs)  # 先落盘再提交：超时后能看出来这批可能已受理
    try:
        task_ids = await canvas.submit(client, request, raw_path=out / RAW / f"{job['id']}-submit.json")
    except NetworkError:
        raise  # 不知道服务端是否已受理：记录停在 submitting
    except WrallyError:
        job["state"] = "failed"  # 服务端明确拒绝（参数、审核），没有扣额度
        save_log(out, jobs)
        raise
    job["state"] = "submitted"
    job["tasks"] = [{"task_id": t, "state": "running"} for t in task_ids]
    save_log(out, jobs)
    report.summary = f"submitted {kind} job {job['id']}: {len(task_ids)} task(s) with {model.name}"
    report.job_ids = [job["id"]]
    return report


# ---------- 等待与下载 ----------


async def fetch(
    client: McpClient,
    out: Path,
    *,
    job_ids: list[str] | None = None,
    timeout: float = 600,
    http: httpx2.AsyncClient | None = None,
) -> Report:
    """等记录里的任务结束并下载结果。job_ids 为空时处理所有没下载完的记录。"""
    jobs = load_log(out)
    if job_ids is None:
        chosen = [j for j in jobs if j["state"] == "submitted"]
    else:  # 点名的记录即使已经下载完也列出来，方便重跑时看到文件
        chosen = [j for j in jobs if j["id"] in job_ids and j["state"] in ("submitted", "done")]
    report = Report()
    deadline = time.monotonic() + timeout
    while True:
        running = [(j, t) for j in chosen for t in j["tasks"] if t["state"] == "running"]
        for job, task in running:
            raw = out / RAW / f"{job['id']}-task-{task['task_id']}.json"
            got = await canvas_task(client, task["task_id"], job.get("project_id"), raw_path=raw)
            if got.state == "done":
                task["state"] = "done"
                task["asset_id"] = got.asset_ids[0] if got.asset_ids else None
                if not task["asset_id"]:
                    task["state"] = "failed"
                    task["error"] = "no_asset"
            elif got.state == "failed":
                task["state"] = "failed"
                task["error"] = got.error
        if running:
            save_log(out, jobs)
        if not any(t["state"] == "running" for j in chosen for t in j["tasks"]):
            break
        if time.monotonic() >= deadline:
            break
        await anyio.sleep(POLL_SECONDS)

    items: list[tuple[str, Path]] = []
    owners: list[dict] = []
    for job in chosen:
        for task, stem in zip(job["tasks"], _files_for(job), strict=True):
            if task["state"] == "done" and not (task.get("file") and (out / task["file"]).exists()):
                items.append((task["asset_id"], out / stem))
                owners.append(task)
    downloads = await download_assets(client, items, raw_path=out / RAW / "asset_detail.json", http=http)
    for task, d in zip(owners, downloads, strict=True):
        if d.ok and d.path:
            task["file"] = d.path.name
        else:
            task["download_error"] = d.error

    done_files = 0
    for job in chosen:
        if all(t["state"] in ("done", "failed") for t in job["tasks"]) and all(
            t["state"] == "failed" or t.get("file") for t in job["tasks"]
        ):
            job["state"] = "done" if any(t["state"] == "done" for t in job["tasks"]) else "failed"
        for task in job["tasks"]:
            if task["state"] == "running":
                report.timed_out += 1
                report.rows.append((job["id"], "running", "-"))
            elif task["state"] == "failed":
                report.failed += 1
                reason = task.get("error")
                report.rows.append((job["id"], f"failed: {reason}" if reason else "failed", "-"))
            elif task.get("file"):
                done_files += 1
                report.rows.append((job["id"], "done", task["file"]))
            else:
                report.failed += 1
                report.rows.append((job["id"], f"error: {task.get('download_error') or 'download'}", "-"))
    save_log(out, jobs)
    report.job_ids = [j["id"] for j in chosen]
    report.summary = (
        f"{done_files} file(s) in {out}, {report.failed} failed, {report.timed_out} still running"
        if chosen
        else "nothing to fetch"
    )
    return report
