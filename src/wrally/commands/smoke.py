"""wrally smoke：真实调用 WorkRally，依次生成一张图、一段配音、一段 5 秒视频，检查下载的文件。

提交、等待和下载复用 wrally gen（commands/gen.py），状态也在 gen 的提交记录里。
每次运行一个目录 .wrally-smoke/<时间戳>/，计划（三步的参数）先写进 smoke.json，
--resume 按这份计划接着做：已提交的步骤由 gen 的参数指纹拦住，不会重复提交。

视频用图像那一步的结果当单图；图像失败时退回纯文字。
模型按 选项 → 合并后的 defaults → BUILTIN_MODELS 的顺序取。
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx2

from wrally import media
from wrally.api import canvas
from wrally.commands import gen as gen_cmd
from wrally.errors import AuthError, NetworkError, PreflightError, WrallyError
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

ROOT = ".wrally-smoke"
PLAN = "smoke.json"
STEPS = ("image", "audio", "video")
VIDEO_SECONDS = 5
DURATION_SLACK = 0.5  # 视频时长允许的误差（秒）
HEADERS = ("step", "model", "state", "secs", "check")

# 没给选项、项目里也没有 defaults 时用的模型：smoke 测的是工具和接口，不是某部片子，
# 所以这里例外地写了名称。图像、视频 2026-10-31 真实跑通过；配音原用 Seed-Audio-1.5，
# 2026-11-01 画布目录里没有了，改用同样支持参考音色的 Seed-Audio-1.0。
# 仍按远端模型目录校验，下线了就报错，不自动换。
BUILTIN_MODELS = {"image": "Seedream-5 Pro", "audio": "Seed-Audio-1.0", "video": "Seedance-2.0 Mini"}

PROMPTS = {
    "image": "一只白色陶瓷杯放在木桌上，侧光，背景简洁。",
    "audio": "用平稳的语气说：「这是一次生成测试。」",
    "video": "镜头缓慢推近桌上的白色陶瓷杯，光线保持不变。",
}


@dataclass
class Report:
    summary: str = ""
    rows: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    run_dir: Path | None = None
    failed: int = 0
    pending: int = 0  # 还在跑或没轮到的步骤


# ---------- 计划 ----------


def _defaults(cwd: Path) -> dict:
    """当前目录可以是创作项目目录（有 wrally.yaml）或单集目录：项目文件打底，清单覆盖。"""
    probe_ws = Workspace(cwd / ROOT)  # 它的上级目录从 cwd 开始找 wrally.yaml
    return probe_ws.settings(Workspace(cwd).load(), "defaults")


def _project_id(cwd: Path, given: str | None) -> str | None:
    if given:
        return given
    data = Workspace(cwd).load() or {}
    return data.get("project_id") or Workspace(cwd / ROOT).project().get("project_id")


def _lowest(values: list[str]) -> int | None:
    nums = sorted(int(v) for v in values if str(v).isdigit())
    return nums[0] if nums else None


def _aspect(model: canvas.Model, wanted: str | None) -> str | None:
    if not model.aspects:
        return wanted
    if wanted in model.aspects:
        return wanted
    return "16:9" if "16:9" in model.aspects else model.aspects[0]


async def plan(client: McpClient, cwd: Path, models: dict[str, str | None], only: list[str]) -> dict:
    """按 defaults 和覆盖的模型定下各步参数，并在提交任何任务前全部校验。"""
    defaults = _defaults(cwd)
    aspect = (defaults.get("image") or {}).get("aspect")
    steps: dict[str, dict] = {}
    for kind in only:
        source, wanted = next(
            (src, str(m))
            for src, m in (
                ("option", models.get(kind)),
                ("defaults", (defaults.get(kind) or {}).get("model")),
                ("built-in", BUILTIN_MODELS[kind]),
            )
            if m
        )
        catalog = await canvas.models(client, kind)
        if source == "built-in" and not any(m.matches(wanted) for m in catalog):
            raise PreflightError(
                "missing_model", f"(built-in {kind} model {wanted} is gone; use --{kind}-model)"
            )
        model = canvas.pick_model(catalog, wanted, kind)
        params: dict = {"prompt": PROMPTS[kind], "model": model.id, "count": 1, "name": kind}
        if kind == "image":
            params |= {"aspect": _aspect(model, aspect), "resolution": _lowest(model.resolutions), "refs": []}
        elif kind == "audio":
            voice = (defaults.get("audio") or {}).get("voice_asset")
            usable = voice and not model.minimax and model.max_refs  # 有参考音色就带上，测的是实际用法
            params["voices"] = [str(voice)] if usable else []
        else:
            if model.durations and VIDEO_SECONDS not in model.durations:
                raise PreflightError("bad_duration", f"({model.name}: no {VIDEO_SECONDS}s option)")
            params |= {"mode": "text", "image": None, "last": None, "duration": VIDEO_SECONDS,
                       "resolution": _lowest(model.resolutions), "aspect": _aspect(model, aspect),
                       "sound": False}  # fmt: skip
        gen_cmd.validate(kind, model, params)
        steps[kind] = {"model_name": model.name, "model_source": source, "params": params}
    return {"version": 1, "steps": steps}


def _describe(kind: str, params: dict, with_image: bool) -> str:
    """演练时说明每步会用的关键参数。"""
    if kind == "audio":
        return "with voice" if params.get("voices") else "no voice"
    bits = [f"{params['duration']}s"] if kind == "video" else []
    bits += [
        f"resolution {params['resolution']}" if params.get("resolution") is not None else "",
        params.get("aspect") or "",
    ]
    if kind == "video":
        bits.append("no sound, from image step" if with_image else "no sound, text only")
    return ", ".join(b for b in bits if b)


def _save_plan(run: Path, data: dict) -> None:
    run.mkdir(parents=True, exist_ok=True)
    tmp = run / (PLAN + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, run / PLAN)


def latest_run(cwd: Path) -> Path | None:
    runs = sorted(p for p in (cwd / ROOT).glob("*") if (p / PLAN).is_file())
    return runs[-1] if runs else None


# ---------- 检查 ----------


def check_file(kind: str, path: Path) -> tuple[bool, str]:
    """下载的文件能被 ffprobe 读出来，视频的时长是 5 秒。返回（是否通过，说明）。"""
    info = media.probe(path)
    if kind == "image":
        if not (info["width"] and info["height"]):
            return False, "unreadable image"
        return True, f"{info['width']}x{info['height']} {path.suffix.lstrip('.')}"
    if kind == "audio":
        if info["duration"] <= 0:
            return False, "no duration"
        return True, f"{info['duration']:.1f}s"
    if not info["video"]:
        return False, "no video stream"
    note = f"{info['duration']:.2f}s, {info['width']}x{info['height']}"
    note += ", audio track" if info["audio"] else ", no audio track"
    if abs(info["duration"] - VIDEO_SECONDS) > DURATION_SLACK:
        return False, f"{note} (want {VIDEO_SECONDS}s)"
    return True, note


# ---------- 运行 ----------


def _job(jobs: list[dict], job_id: str | None) -> dict | None:
    return next((j for j in jobs if j["id"] == job_id), None)


def _secs(job: dict, step: dict) -> str:
    if not step.get("done_at") or not job.get("submitted_at"):
        return "-"
    start = datetime.fromisoformat(job["submitted_at"])
    return str(int((datetime.fromisoformat(step["done_at"]) - start).total_seconds()))


async def run(
    client: McpClient,
    cwd: Path,
    *,
    models: dict[str, str | None] | None = None,
    only: list[str] | None = None,
    project_id: str | None = None,
    resume: bool = False,
    dry_run: bool = False,
    timeout: float = 1800,
    http: httpx2.AsyncClient | None = None,
) -> Report:
    report = Report()
    if resume:
        run_dir = latest_run(cwd)
        if run_dir is None:
            raise PreflightError("no_run", f"(nothing to resume in {cwd / ROOT})")
        data = json.loads((run_dir / PLAN).read_text(encoding="utf-8"))
    else:
        chosen = only or list(STEPS)
        bad = [k for k in chosen if k not in STEPS]
        if bad:
            raise PreflightError("bad_step", f"({','.join(bad)}; allowed: {', '.join(STEPS)})")
        data = await plan(client, cwd, models or {}, [k for k in STEPS if k in chosen])
        data["project_id"] = _project_id(cwd, project_id)
        if dry_run:
            report.summary = f"dry run: would submit {len(data['steps'])} job(s)"
            report.rows = [
                (
                    k,
                    f"{s['model_name']} ({s['model_source']})",
                    "planned",
                    "-",
                    _describe(k, s["params"], "image" in data["steps"]),
                )
                for k, s in data["steps"].items()
            ]
            return report
        run_dir = cwd / ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
        _save_plan(run_dir, data)
    report.run_dir = run_dir
    # --resume --dry-run 只看进度：不提交新任务，也不等待
    deadline = time.monotonic() + (0 if dry_run else timeout)
    project = data.get("project_id")
    image_asset: str | None = None
    stopped = False
    for kind, step in data["steps"].items():
        name = step["model_name"]
        if stopped:
            report.pending += 1
            report.rows.append((kind, name, "not started", "-", "-"))
            continue
        if step.get("job_id") and not dry_run:
            prior = _job(gen_cmd.load_log(run_dir), step["job_id"])
            if prior and prior["state"] == "failed":  # 上次整条失败（远端繁忙等），没扣额度：重新提交
                step.pop("job_id")
                step.pop("done_at", None)
        if not step.get("job_id"):
            if dry_run:
                report.pending += 1
                report.rows.append((kind, name, "not started", "-", "-"))
                stopped = True
                continue
            params = dict(step["params"])
            if kind == "video":
                params["image"] = image_asset  # 接图像那一步的结果；没有就是纯文字
            try:
                submitted = await gen_cmd.gen(client, kind, params, run_dir, project_id=project, http=http)
            except (NetworkError, AuthError, PreflightError):
                raise  # 不确定是否已受理，或前置条件不满足：整条命令停下
            except WrallyError as e:  # 服务端明确拒绝，没有扣额度：记下，接着做下一步
                report.failed += 1
                report.rows.append((kind, name, f"failed: {e.code}", "-", "-"))
                continue
            step["job_id"] = submitted.job_ids[0]
            step["image"] = params.get("image") if kind == "video" else None
            _save_plan(run_dir, data)
        left = max(0.0, deadline - time.monotonic())
        await gen_cmd.fetch(client, run_dir, job_ids=[step["job_id"]], timeout=left, http=http)
        job = _job(gen_cmd.load_log(run_dir), step["job_id"]) or {"tasks": []}
        task = job["tasks"][0] if job["tasks"] else {}
        if task.get("state") == "running":
            report.pending += 1
            report.rows.append((kind, name, "running", "-", "-"))
            stopped = True  # 按顺序做：视频要等图，后面的步骤留给 --resume
            continue
        if task.get("file"):
            ok, note = check_file(kind, run_dir / task["file"])
            if "done_at" not in step:
                step["done_at"] = datetime.now().isoformat(timespec="seconds")
                _save_plan(run_dir, data)
            if kind == "video" and not step.get("image"):
                note += ", text only"
            state = "done" if ok else "failed: check"
            if ok and kind == "image":
                image_asset = task.get("asset_id")
        else:
            ok, state = False, "failed"
            note = task.get("error") or task.get("download_error") or "no result"
        if not ok:
            report.failed += 1
        report.rows.append((kind, name, state, _secs(job, step), str(note)))
    passed = sum(1 for r in report.rows if r[2] == "done")
    report.summary = f"smoke: {passed}/{len(report.rows)} passed ({run_dir})"
    if report.pending:
        report.summary += "; run `wrally smoke --resume` to continue"
    return report
