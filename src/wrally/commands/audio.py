"""audio gen：生成旁白。

顺序：本地前置检查 → 逐镜绑定音色（已绑定的跳过）→ 批量写 audio_prompt → 一次 set_model audio
→ 回读核对 → 一次提交。旁白提示词由 audio gen 自己写入，不要求事先 push。

幂等和 video gen 同一套：每镜记下输入指纹（台词拼出的提示词 + 模型 + 音色），没变不重复提交。
--redo-failed 只重做检查没过的镜头：音高超标、左右声道音量差太大
（check 写进清单的 f0、channel_gap）或上次生成失败。
"""

import hashlib
import json
import time

import httpx2

from wrally.api import shots as shots_api
from wrally.api.audio import bind_voice, submit_audio
from wrally.api.results import get_results
from wrally.commands.common import for_each, is_skipped, load_manifest, remember_web_copies, section, select
from wrally.commands.video import GenReport
from wrally.errors import PreflightError, WrallyError
from wrally.prompts import composed, load_styles
from wrally.rules import Rules, load_rules
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("shot", "status", "detail")


def fingerprint(prompt: str, model: str, voice: str) -> str:
    payload = json.dumps([prompt, model, voice], ensure_ascii=False)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _keys(entries: list[dict]) -> str:
    return ",".join(str(e["key"]) for e in entries)


def needs_redo(entry: dict, rules: Rules) -> str:
    """这一镜的旁白是否没过检查；返回原因，没问题时返回空串。"""
    audio = entry.get("audio") or {}
    if audio.get("status") == "failed":
        return "last run failed"
    return rules.failed_audio(audio)


async def gen(
    ws: Workspace,
    client: McpClient,
    spec: str = "all",
    *,
    redo: bool = False,
    redo_failed: bool = False,
    dry_run: bool = False,
    http: httpx2.AsyncClient | None = None,
) -> GenReport:
    data = load_manifest(ws)
    selected = select(data, spec)
    styles = load_styles(ws, data)
    defaults = ws.settings(data, "defaults").get("audio") or {}
    rules = load_rules(ws, data)
    model, voice, voice_name = defaults.get("model"), defaults.get("voice_asset"), defaults.get("voice_name")
    missing = [n for n, v in (("model", model), ("voice_asset", voice), ("voice_name", voice_name)) if not v]
    if missing:
        raise PreflightError("missing_defaults", f"(defaults.audio.{missing[0]})")
    series_id, project_id = data["series_id"], data["project_id"]
    report = GenReport(noun="audio")
    for e in [e for e in selected if is_skipped(e, "audio")]:
        report.rows.append((str(e["key"]), "skipped", "marked skip in episode.yaml"))
    entries = [e for e in selected if not is_skipped(e, "audio")]

    # —— 本地前置检查 ——
    prompts = {str(e["key"]): composed(ws, styles, e, "audio") for e in entries}
    no_text = [e for e in entries if not (prompts[str(e["key"])] or "").strip()]
    if no_text:
        raise PreflightError("missing_narration", f"({_keys(no_text)})")
    prints = {k: fingerprint(p, model, voice) for k, p in prompts.items()}

    todo, recover = [], []
    for e in entries:
        key, audio = str(e["key"]), e.get("audio") or {}
        state, same = audio.get("status"), audio.get("made_from") == prints[key]
        why = needs_redo(e, rules) if redo_failed else ""
        if redo or why:
            todo.append(e)
        elif state == "submitting" and same:
            recover.append(e)
        elif state in ("running", "done") and same:
            report.rows.append((key, "skipped", f"{state}, inputs unchanged"))
        elif state == "running":
            report.rows.append((key, "skipped", "another task is running; wait for it first"))
        elif state == "done" and not audio.get("made_from"):
            report.rows.append((key, "skipped", "done, made outside wrally (--redo to regenerate)"))
        else:
            todo.append(e)
    if dry_run:
        report.dry_run = True
        report.rows += [
            (str(e["key"]), "would submit", needs_redo(e, rules) if redo_failed else "") for e in todo
        ]
        report.rows += [(str(e["key"]), "would check", "interrupted run; remote decides") for e in recover]
        return report
    if not todo and not recover:
        return report

    # —— 远端状态 ——
    remote = {
        s.story_id: s for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "audio")
    }
    remember_web_copies(ws, remote.values())
    gone = [e for e in todo + recover if e["story_id"] not in remote]
    if gone:
        raise PreflightError("missing_on_remote", f"({_keys(gone)})")

    async def count(entry: dict):
        return await get_results(
            client, entry["story_id"], "audio", ws.raw_dir / f"{entry['key']}-audio-before.json"
        )

    counts = dict(
        zip([str(e["key"]) for e in todo + recover], await for_each(todo + recover, count), strict=True)
    )
    broken = [k for k, c in counts.items() if isinstance(c, WrallyError)]
    if broken:
        raise PreflightError("cannot_read_results", f"({','.join(broken)})")
    for e in recover:
        key, audio = str(e["key"]), section(e, "audio")
        rs = counts[key]
        if rs.doing > 0 or rs.done > int(audio.get("results_before") or 0):
            audio["status"] = "running" if rs.doing > 0 else "done"
            report.rows.append((key, "skipped", "already submitted by an interrupted run"))
        else:
            todo.append(e)
    todo.sort(key=lambda e: [str(x["key"]) for x in entries].index(str(e["key"])))
    if not todo:
        ws.save(data)
        return report
    capabilities = await shots_api.model_capabilities(
        client, "audio", model, ws.raw_dir / "audio-models.json"
    )

    # —— 逐镜绑定音色：已经绑着同一个音色的不动 ——
    unbound = [e for e in todo if remote[e["story_id"]].voice_asset_ids != [voice]]
    bind_errors = await bind_voice(
        client, [(str(e["key"]), e["story_id"]) for e in unbound], voice, voice_name, ws.raw_dir, http
    )
    ready = []
    for e in todo:
        key = str(e["key"])
        if bind_errors.get(key):
            report.failed += 1
            report.rows.append(
                (key, f"error: {bind_errors[key]}", f"(raw: {ws.raw_dir / f'{key}-audio-bind_asset.json'})")
            )
        else:
            ready.append(e)
    if not ready:
        return report

    # —— 批量写 audio_prompt → 一次 set_model audio（顺序不能反）——
    edits = [
        {"story_id": e["story_id"], "audio_prompt": prompts[str(e["key"])]}
        for e in ready
        if remote[e["story_id"]].audio_prompt.strip("\n") != prompts[str(e["key"])]
    ]
    if edits:
        await shots_api.edit_content(client, series_id, project_id, edits, ws.raw_dir)
    config = {"model": model, "count": 1, "modelCapabilities": capabilities}
    await shots_api.set_model(
        client, series_id, project_id, [e["story_id"] for e in ready], "audio", config,
        ws.raw_dir / "audio-set_model.json",
    )  # fmt: skip

    # —— 回读核对 ——
    after = {
        s.story_id: s
        for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "audio-verify")
    }
    wrong = [
        e for e in ready
        if after[e["story_id"]].audio_prompt.strip("\n") != prompts[str(e["key"])]
        or (after[e["story_id"]].gen_config.get("audio") or {}).get("model") != model
        or after[e["story_id"]].voice_asset_ids != [voice]
    ]  # fmt: skip
    if wrong:
        raise PreflightError("config_not_applied", f"({_keys(wrong)})")

    # audio_prompt 和模型配置存在同一个字段里：写旁白有可能把视频配置冲掉，冲掉了要让人知道
    def video_of(shots: dict, entry: dict) -> object:
        return shots[entry["story_id"]].gen_config.get("video")

    lost = [e for e in ready if video_of(after, e) != video_of(remote, e)]
    notes = [f"note: video config changed on {_keys(lost)} (video gen rewrites it)"] if lost else []

    # —— 提交：先落盘 submitting，再发请求，成功后立刻记下任务 ID ——
    for e in ready:
        key, audio = str(e["key"]), section(e, "audio")
        audio.pop("task", None)
        audio["status"] = "submitting"
        audio["made_from"] = prints[key]
        audio["results_before"] = counts[key].done
        audio["failed_before"] = counts[key].failed
    ws.save(data)
    raw = ws.raw_dir / "audio-submit.json"
    result = await submit_audio(client, project_id, series_id, [e["story_id"] for e in ready], raw)
    skipped = json.dumps(result["skipped"], ensure_ascii=False)
    accepted = [e for e in ready if e["story_id"] not in skipped]
    tasks = result["task_ids"] if len(result["task_ids"]) == len(accepted) else [None] * len(accepted)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for e in ready:
        key, audio = str(e["key"]), section(e, "audio")
        if e not in accepted:
            audio["status"] = "failed"
            report.failed += 1
            report.rows.append((key, "error: skipped_by_server", f"(raw: {raw})"))
            continue
        audio["status"] = "running"
        audio["submitted_at"] = stamp
        task = tasks[accepted.index(e)]
        if task:
            audio["task"] = task
        report.submitted.append(key)
        report.rows.append((key, "submitted", str(task or "")))
    ws.save(data)
    report.rows.sort(key=lambda r: not r[1].startswith("error"))
    report.notes = notes
    return report
