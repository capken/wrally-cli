"""audio gen：生成旁白。

顺序：本地前置检查 → 逐镜绑定音色（已绑定的跳过）→ 批量写 audio_prompt → 一次 set_model audio
→ 回读核对 → 一次提交。旁白提示词由 audio gen 自己写入，不要求事先 push。

幂等和 video gen 同一套：每镜记下输入指纹（台词拼出的提示词 + 模型 + 音色），没变不重复提交。
音色有两种给法：参考音色（voice_asset + voice_name，逐镜绑定参考音频，旁白模板用 <音色名> 引用；
voice_asset 可以写本地音频文件，绑定前上传成媒资，指纹按文件内容算），
或预置音色（voice_preset，MiniMax 这类模型自带的音色 ID，写进 set_model 的 audioFieldValues.voice，
不绑定参考音频）。预置音色的模型会把提示词原样念出来，所以这时不能配旁白模板，提示词就是台词本身；
voice_fields 里的其余字段（如 audio_speech_rate）原样并进 audioFieldValues。

--redo-failed 只重做检查没过的镜头：音高超标、左右声道音量差太大、停顿处有背景音
（check 写进清单的 f0、channel_gap、noise_floor）或上次生成失败。
"""

import hashlib
import json

import httpx2

from wrally.api import shots as shots_api
from wrally.api.audio import bind_voice, submit_audio
from wrally.commands import upload as upload_cmd
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
from wrally.errors import PreflightError
from wrally.prompts import composed, load_styles
from wrally.rules import Rules, load_rules
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

__all__ = ["HEADERS", "GenReport", "fingerprint", "gen", "needs_redo", "shot_model", "voice_key"]


def fingerprint(prompt: str, model: str, voice: str) -> str:
    payload = json.dumps([prompt, model, voice], ensure_ascii=False)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def voice_key(ws: Workspace, audio_defaults: dict) -> str:
    """音色的身份，进输入指纹：参考音色是媒资 ID（本地文件按内容），预置音色是 preset:<ID> 加上额外字段。"""
    preset = audio_defaults.get("voice_preset")
    fields = audio_defaults.get("voice_fields") or {}
    if not preset:
        asset = audio_defaults.get("voice_asset")
        asset = upload_cmd.print_of(ws, asset) if asset else ""
        # 不写 voice_fields 时保持原样，旧清单的指纹不变
        return asset + ("|" + json.dumps(fields, ensure_ascii=False, sort_keys=True) if fields else "")
    return "preset:" + json.dumps({"voice": preset, **fields}, ensure_ascii=False, sort_keys=True)


def shot_model(model: str, entry: dict) -> str:
    """单镜的旁白模型：这一镜写了 `audio_model` 就用它，否则用 defaults.audio.model。"""
    value = entry.get("audio_model")
    if value is None:
        return model
    if not isinstance(value, str) or not value.strip():
        raise PreflightError("bad_audio_model", f"({entry.get('key')}: {value!r})")
    return value.strip()


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
    preset = defaults.get("voice_preset")
    needed = (
        (("model", model),)
        if preset
        else (("model", model), ("voice_asset", voice), ("voice_name", voice_name))
    )
    missing = [n for n, v in needed if not v]
    if missing:
        raise PreflightError("missing_defaults", f"(defaults.audio.{missing[0]})")
    if preset and styles.audio is not None:
        # 预置音色的模型把提示词原样念出来，模板里的「使用<…>的音色」也会被念进去
        raise PreflightError(
            "template_with_preset", "(set defaults.narration_template: null when using voice_preset)"
        )
    # 预置音色写进 voice；voice_fields（如 audio_speech_rate）参考音色也能用
    field_values = ({"voice": preset} if preset else {}) | (defaults.get("voice_fields") or {}) or None
    voice_id = voice_key(ws, defaults)
    series_id, project_id = data["series_id"], data["project_id"]
    report = GenReport(noun="audio")
    entries = skip_marked(selected, "audio", report)

    # —— 本地前置检查 ——
    prompts = {str(e["key"]): composed(ws, styles, e, "audio") for e in entries}
    no_text = [e for e in entries if not (prompts[str(e["key"])] or "").strip()]
    if no_text:
        raise PreflightError("missing_narration", f"({join_keys(no_text)})")
    models = {str(e["key"]): shot_model(model, e) for e in entries}
    prints = {k: fingerprint(p, models[k], voice_id) for k, p in prompts.items()}

    todo, recover = classify(
        entries,
        prints,
        "audio",
        report,
        redo=redo,
        dry_run=dry_run,
        redo_reason=(lambda e: needs_redo(e, rules)) if redo_failed else None,
    )
    if dry_run or (not todo and not recover):
        return report

    # —— 远端状态与中断恢复 ——
    state = await inspect_and_recover(ws, client, data, entries, todo, recover, "audio", report)
    if not state.todo:
        return report
    remote, counts, todo = state.remote, state.counts, state.todo

    capabilities = {}
    for m in dict.fromkeys(models[str(e["key"])] for e in todo):
        name = "audio-models.json" if m == model else f"audio-models-{m}.json"
        capabilities[m] = await shots_api.model_capabilities(client, "audio", m, ws.raw_dir / name)

    if not preset and ws.media_file(voice):
        # 音色写的是本地文件：先上传，之后按媒资 ID 绑定和核对
        voice = (await upload_cmd.resolve(client, [voice], ws.root, project_id, http=http)).ids[voice]

    # —— 逐镜绑定音色：已经绑着同一个音色的不动；预置音色不绑定 ——
    unbound = [] if preset else [e for e in todo if remote[e["story_id"]].voice_asset_ids != [voice]]
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
    # 一次 set_model 只能写一个模型：按模型分组，每组一次
    for m in dict.fromkeys(models[str(e["key"])] for e in ready):
        group = [e["story_id"] for e in ready if models[str(e["key"])] == m]
        config = {"model": m, "count": 1, "modelCapabilities": capabilities[m]}
        if field_values:
            config["audioFieldValues"] = field_values
        name = "audio-set_model.json" if m == model else f"audio-set_model-{m}.json"
        await shots_api.set_model(client, series_id, project_id, group, "audio", config, ws.raw_dir / name)

    # —— 回读核对 ——
    after = {
        s.story_id: s
        for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "audio-verify")
    }
    wrong = [
        e for e in ready
        if after[e["story_id"]].audio_prompt.strip("\n") != prompts[str(e["key"])]
        or (after[e["story_id"]].gen_config.get("audio") or {}).get("model") != models[str(e["key"])]
        or (not preset and after[e["story_id"]].voice_asset_ids != [voice])
        or (preset and (after[e["story_id"]].gen_config.get("audio") or {}).get("audioFieldValues")
            != field_values)
    ]  # fmt: skip
    if wrong:
        raise PreflightError("config_not_applied", f"({join_keys(wrong)})")

    # audio_prompt 和模型配置存在同一个字段里：写旁白有可能把视频配置冲掉，冲掉了要让人知道
    def video_of(shots: dict, entry: dict) -> object:
        return shots[entry["story_id"]].gen_config.get("video")

    lost = [e for e in ready if video_of(after, e) != video_of(remote, e)]
    notes = [f"note: video config changed on {join_keys(lost)} (video gen rewrites it)"] if lost else []

    # —— 提交：先落盘 submitting，再发请求，成功后立刻记下任务 ID ——
    mark_submitting(ws, data, ready, "audio", prints, counts)
    raw = ws.raw_dir / "audio-submit.json"
    result = await submit_audio(client, project_id, series_id, [e["story_id"] for e in ready], raw)
    record_batch_submit(ws, data, ready, "audio", result, raw, report)
    report.rows.sort(key=lambda r: not r[1].startswith("error"))
    report.notes = notes
    return report
