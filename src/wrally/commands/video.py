"""video gen：按选定关键帧生成视频（单图模式）。

顺序：本地前置检查 → 远端前置检查（提示词已 push、选图没有冲突）→ 逐镜写配置 → 回读核对 → 一次提交。
任何一步前置检查不过都以退出码 2 结束，不提交任务。

幂等：每镜记下「这条视频是用什么做的」（关键帧 + 提示词 + 参数的指纹）。重跑时指纹没变就不再提交；
提交前先把状态写成 submitting，命令在提交途中被打断，重跑时靠远端的结果计数判断任务是否已受理。
"""

import hashlib
import json
import time
from dataclasses import dataclass, field, replace

import httpx2

from wrally.api import shots as shots_api
from wrally.api.results import get_results
from wrally.api.video import VideoSettings, configure_single_image, submit_video
from wrally.commands.common import for_each, is_skipped, load_manifest, remember_web_copies, section, select
from wrally.commands.verify import same_assets
from wrally.errors import PreflightError, WrallyError
from wrally.prompts import composed, load_styles
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("shot", "status", "detail")


@dataclass
class GenReport:
    rows: list[tuple[str, ...]] = field(default_factory=list)
    submitted: list[str] = field(default_factory=list)
    failed: int = 0
    noun: str = "video"
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        skipped = sum(r[1] == "skipped" for r in self.rows)
        tail = f", {skipped} skipped" if skipped else ""
        tail += f", {self.failed} failed" if self.failed else ""
        if self.dry_run:
            would = [r[0] for r in self.rows if r[1] == "would submit"]
            return f"dry run: would submit {len(would)} {self.noun} tasks  ({' '.join(would) or '-'}){tail}"
        return f"submitted {len(self.submitted)} {self.noun} tasks  ({' '.join(self.submitted) or '-'}){tail}"


def video_settings(defaults: dict) -> VideoSettings:
    video = defaults.get("video") or {}
    if not video.get("model"):
        raise PreflightError("missing_defaults", "(defaults.video.model)")
    return VideoSettings(
        model=str(video["model"]),
        aspect=str(video.get("aspect") or (defaults.get("image") or {}).get("aspect") or "16:9"),
        duration=int(video.get("duration") or 8),
        resolution=int(video.get("resolution") or 1),
        audio=bool(video.get("audio", False)),
    )


def shot_settings(settings: VideoSettings, entry: dict) -> VideoSettings:
    """单镜的视频参数：清单里这一镜写了 `video_duration` 就用它，其余沿用 defaults.video。"""
    duration = entry.get("video_duration")
    if duration is None:
        return settings
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        raise PreflightError("bad_video_duration", f"({entry.get('key')}: {duration!r})")
    return replace(settings, duration=duration)


def fingerprint(asset_id: str, prompt: str, settings: VideoSettings) -> str:
    payload = json.dumps([asset_id, prompt, settings.__dict__], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _keys(entries: list[dict]) -> str:
    return ",".join(str(e["key"]) for e in entries)


async def gen(
    ws: Workspace,
    client: McpClient,
    spec: str = "all",
    *,
    redo: bool = False,
    dry_run: bool = False,
    force_picks: bool = False,
    http: httpx2.AsyncClient | None = None,
) -> GenReport:
    data = load_manifest(ws)
    entries = select(data, spec)
    styles = load_styles(ws, data)
    settings = video_settings(ws.settings(data, "defaults"))
    series_id, project_id = data["series_id"], data["project_id"]
    report = GenReport()
    for e in [e for e in entries if is_skipped(e, "video")]:
        report.rows.append((str(e["key"]), "skipped", "marked skip in episode.yaml"))
    entries = [e for e in entries if not is_skipped(e, "video")]

    # —— 本地前置检查 ——
    no_pick = [e for e in entries if not (e.get("keyframe") or {}).get("asset_id")]
    if no_pick:
        raise PreflightError("missing_asset_id", f"({_keys(no_pick)}; run: wrally keyframe pick)")
    prompts = {str(e["key"]): composed(ws, styles, e, "anim") for e in entries}
    no_prompt = [e for e in entries if not (prompts[str(e["key"])] or "").strip()]
    if no_prompt:
        raise PreflightError("missing_anim_prompt", f"({_keys(no_prompt)})")
    per_shot = {str(e["key"]): shot_settings(settings, e) for e in entries}
    prints = {
        str(e["key"]): fingerprint(e["keyframe"]["asset_id"], prompts[str(e["key"])], per_shot[str(e["key"])])
        for e in entries
    }

    todo, recover = [], []
    for e in entries:
        key = str(e["key"])
        video = e.get("video") or {}
        state, same = video.get("status"), video.get("made_from") == prints[key]
        if redo:
            todo.append(e)
        elif state == "submitting" and same:
            recover.append(e)  # 上次在提交途中被打断，先问远端
        elif state in ("running", "done") and same:
            report.rows.append((key, "skipped", f"{state}, inputs unchanged"))
        elif state == "running":
            report.rows.append((key, "skipped", "another task is running; wait for it first"))
        elif state == "done" and not video.get("made_from"):
            report.rows.append((key, "skipped", "done, made outside wrally (--redo to regenerate)"))
        else:
            todo.append(e)
    if dry_run:
        # 只做本地判断：列出会提交哪些镜头，不调用远端
        report.dry_run = True
        report.rows += [(str(e["key"]), "would submit", "") for e in todo]
        report.rows += [(str(e["key"]), "would check", "interrupted run; remote decides") for e in recover]
        return report
    if not todo and not recover:
        return report

    # —— 远端前置检查 ——
    remote = {
        s.story_id: s for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "video")
    }
    remember_web_copies(ws, remote.values())
    gone = [e for e in todo + recover if e["story_id"] not in remote]
    if gone:
        raise PreflightError("missing_on_remote", f"({_keys(gone)})")

    async def count(entry: dict):
        return await get_results(
            client, entry["story_id"], "video", ws.raw_dir / f"{entry['key']}-video-before.json"
        )

    counts = dict(
        zip([str(e["key"]) for e in todo + recover], await for_each(todo + recover, count), strict=True)
    )
    broken = [k for k, c in counts.items() if isinstance(c, WrallyError)]
    if broken:
        raise PreflightError("cannot_read_results", f"({','.join(broken)})")
    for e in recover:
        key, video = str(e["key"]), section(e, "video")
        rs = counts[key]
        if rs.doing > 0 or rs.done > int(video.get("results_before") or 0):
            video["status"] = "running" if rs.doing > 0 else "done"
            report.rows.append((key, "skipped", "already submitted by an interrupted run"))
        else:
            todo.append(e)
    todo.sort(key=lambda e: [str(x["key"]) for x in entries].index(str(e["key"])))
    if not todo:
        ws.save(data)
        return report

    unpushed = [
        e for e in todo if remote[e["story_id"]].animation_prompt.strip("\n") != prompts[str(e["key"])]
    ]
    if unpushed:
        raise PreflightError("unpushed_prompts", f"({_keys(unpushed)}; run: wrally push)")
    # 在网页上保存过的镜头另有一份动画提示词，场次生视频用的是那一份，edit_content 改不到它
    stale_web = [e for e in todo if remote[e["story_id"]].rich_anim_differs(prompts[str(e["key"])])]
    if stale_web:
        raise PreflightError("stale_web_prompt", f"({_keys(stale_web)}; paste the anim prompt on the web)")

    # 网页上的选用被人改过，和清单对不上：中止，不替人决定用哪张
    suspects = []
    for e in todo:
        kf = e["keyframe"]
        now = remote[e["story_id"]].keyframe_asset_id
        expected = kf.get("synced") or kf["asset_id"]
        if now and now not in (expected, kf["asset_id"]):
            suspects.append((e, expected, now))
    if suspects and not force_picks:
        verdicts = await same_assets(ws, client, [(a, b) for _, a, b in suspects], http)
        conflicts = [e for e, a, b in suspects if verdicts[(a, b)] is not True]
        if conflicts:
            raise PreflightError("pick_conflict", f"({_keys(conflicts)}; run: wrally verify-picks)")

    # —— 逐镜写配置 → 回读核对 ——
    items = [(str(e["key"]), e["story_id"], e["keyframe"]["asset_id"]) for e in todo]
    errors = await configure_single_image(client, series_id, project_id, items, per_shot, ws.raw_dir, http)
    ready = []
    for e in todo:
        key = str(e["key"])
        if errors[key]:
            report.failed += 1
            report.rows.append(
                (key, f"error: {errors[key]}", f"(raw: {ws.raw_dir / f'{key}-video-set_model.json'})")
            )
        else:
            e["keyframe"]["synced"] = e["keyframe"]["asset_id"]
            ready.append(e)
    ws.save(data)
    if not ready:
        return report
    after = {
        s.story_id: s
        for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "video-verify")
    }
    wrong = [
        e for e in ready
        if after[e["story_id"]].keyframe_asset_id != e["keyframe"]["asset_id"]
        or (after[e["story_id"]].gen_config.get("video") or {}).get("mode") != "Text"
    ]  # fmt: skip
    if wrong:
        raise PreflightError("config_not_applied", f"({_keys(wrong)})")

    # —— 提交：先落盘 submitting，再发请求，成功后立刻记下任务 ID ——
    for e in ready:
        key, video = str(e["key"]), section(e, "video")
        video.pop("task", None)
        video["status"] = "submitting"
        video["made_from"] = prints[key]
        video["results_before"] = counts[key].done
        video["failed_before"] = counts[key].failed
    ws.save(data)
    result = await submit_video(
        client, project_id, series_id, [e["story_id"] for e in ready], ws.raw_dir / "video-submit.json"
    )
    skipped = json.dumps(result["skipped"], ensure_ascii=False)
    accepted = [e for e in ready if e["story_id"] not in skipped]
    tasks = result["task_ids"] if len(result["task_ids"]) == len(accepted) else [None] * len(accepted)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for e in ready:
        key, video = str(e["key"]), section(e, "video")
        if e not in accepted:
            video["status"] = "failed"
            report.failed += 1
            report.rows.append(
                (key, "error: skipped_by_server", f"(raw: {ws.raw_dir / 'video-submit.json'})")
            )
            continue
        video["status"] = "running"
        video["submitted_at"] = stamp
        task = tasks[accepted.index(e)]
        if task:
            video["task"] = task
        report.submitted.append(key)
        report.rows.append((key, "submitted", str(task or "")))
    ws.save(data)
    report.rows.sort(key=lambda r: not r[1].startswith("error"))
    return report
