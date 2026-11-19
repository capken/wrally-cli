"""video gen：按选定关键帧生成视频。没选尾帧的镜头走单图模式，选了尾帧（keyframe.last）的走首尾帧模式。

顺序：本地前置检查 → 远端前置检查（提示词已 push、选图没有冲突）→ 逐镜写配置 → 回读核对 → 一次提交。
任何一步前置检查不过都以退出码 2 结束，不提交任务。

幂等：每镜记下「这条视频是用什么做的」（关键帧 + 提示词 + 参数的指纹）。重跑时指纹没变就不再提交；
提交前先把状态写成 submitting，命令在提交途中被打断，重跑时靠远端的结果计数判断任务是否已受理。
"""

import hashlib
import json
from dataclasses import replace

import httpx2

from wrally.api import shots as shots_api
from wrally.api.video import VideoSettings, configure, submit_video
from wrally.commands.common import load_manifest, select
from wrally.commands.shot_gen import (
    HEADERS,
    GenReport,
    classify,
    inspect_and_recover,
    join_keys,
    mark_submitting,
    record_batch_submit,
    skip_marked,
)
from wrally.commands.verify import same_assets
from wrally.errors import PreflightError
from wrally.prompts import composed, load_styles
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

__all__ = ["HEADERS", "GenReport", "fingerprint", "gen", "last_frame", "shot_settings", "video_settings"]


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
        firstlast_model=str(video["firstlast_model"]) if video.get("firstlast_model") else None,
    )


def last_frame(entry: dict) -> str | None:
    """这一镜选用的尾帧（keyframe pick --last 写入）；没有就是单图模式。"""
    return (entry.get("keyframe") or {}).get("last") or None


def shot_settings(settings: VideoSettings, entry: dict) -> VideoSettings:
    """单镜的视频参数：这一镜写了 `video_duration`、`video_resolution` 就用它，其余沿用 defaults.video。
    选了尾帧的镜头换用 defaults.video.firstlast_model（没写就还用 model）。"""
    if last_frame(entry) and settings.firstlast_model:
        settings = replace(settings, model=settings.firstlast_model)
    for field_, attr, code in (
        ("video_duration", "duration", "bad_video_duration"),
        ("video_resolution", "resolution", "bad_video_resolution"),
    ):
        value = entry.get(field_)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PreflightError(code, f"({entry.get('key')}: {value!r})")
        settings = replace(settings, **{attr: value})
    return settings


def fingerprint(asset_id: str, prompt: str, settings: VideoSettings, last: str | None = None) -> str:
    # 只放实际用到的参数：firstlast_model 已经换进 model；单图镜头的指纹和加首尾帧之前一样
    params = {k: v for k, v in settings.__dict__.items() if k != "firstlast_model"}
    inputs = [asset_id, prompt, params] + ([{"last": last}] if last else [])
    payload = json.dumps(inputs, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


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
    selected = select(data, spec)
    styles = load_styles(ws, data)
    settings = video_settings(ws.settings(data, "defaults"))
    series_id, project_id = data["series_id"], data["project_id"]
    report = GenReport()
    entries = skip_marked(selected, "video", report)

    # —— 本地前置检查 ——
    no_pick = [e for e in entries if not (e.get("keyframe") or {}).get("asset_id")]
    if no_pick:
        raise PreflightError("missing_asset_id", f"({join_keys(no_pick)}; run: wrally keyframe pick)")
    prompts = {str(e["key"]): composed(ws, styles, e, "anim") for e in entries}
    no_prompt = [e for e in entries if not (prompts[str(e["key"])] or "").strip()]
    if no_prompt:
        raise PreflightError("missing_anim_prompt", f"({join_keys(no_prompt)})")
    per_shot = {str(e["key"]): shot_settings(settings, e) for e in entries}
    prints = {
        str(e["key"]): fingerprint(
            e["keyframe"]["asset_id"], prompts[str(e["key"])], per_shot[str(e["key"])], last_frame(e)
        )
        for e in entries
    }

    todo, recover = classify(entries, prints, "video", report, redo=redo, dry_run=dry_run)
    if dry_run or (not todo and not recover):
        return report

    # —— 远端前置检查与中断恢复 ——
    state = await inspect_and_recover(ws, client, data, entries, todo, recover, "video", report)
    if not state.todo:
        return report
    remote, counts, todo = state.remote, state.counts, state.todo

    unpushed = [
        e for e in todo if remote[e["story_id"]].animation_prompt.strip("\n") != prompts[str(e["key"])]
    ]
    if unpushed:
        raise PreflightError("unpushed_prompts", f"({join_keys(unpushed)}; run: wrally push)")
    # 在网页上保存过的镜头另有一份动画提示词，场次生视频用的是那一份，edit_content 改不到它
    stale_web = [e for e in todo if remote[e["story_id"]].rich_anim_differs(prompts[str(e["key"])])]
    if stale_web:
        raise PreflightError(
            "stale_web_prompt", f"({join_keys(stale_web)}; paste the anim prompt on the web)"
        )

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
            raise PreflightError("pick_conflict", f"({join_keys(conflicts)}; run: wrally verify-picks)")

    # —— 逐镜写配置 → 回读核对 ——
    items = [(str(e["key"]), e["story_id"], e["keyframe"]["asset_id"], last_frame(e)) for e in todo]
    errors = await configure(client, series_id, project_id, items, per_shot, ws.raw_dir, http)
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
        or (after[e["story_id"]].gen_config.get("video") or {}).get("mode")
        != ("FirstLastFrame" if last_frame(e) else "Text")
    ]  # fmt: skip
    if wrong:
        raise PreflightError("config_not_applied", f"({join_keys(wrong)})")

    # —— 提交：先落盘 submitting，再发请求，成功后立刻记下任务 ID ——
    mark_submitting(ws, data, ready, "video", prints, counts)
    raw = ws.raw_dir / "video-submit.json"
    result = await submit_video(client, project_id, series_id, [e["story_id"] for e in ready], raw)
    record_batch_submit(ws, data, ready, "video", result, raw, report)
    report.rows.sort(key=lambda r: not r[1].startswith("error"))
    return report
