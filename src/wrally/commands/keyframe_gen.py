"""keyframe gen：生成关键帧候选。

普通镜头：set_model image → shotlist_generate_image（全部镜头一次提交）。
带 refs 的镜头：用被引用镜头的定稿关键帧作参考图，走画布图生图，逐镜提交。
refs 里的一项可以是镜号（取那一镜的定稿关键帧），也可以是 ref_assets 小节里登记的名字（取对应的媒资，
比如角色设定图）。镜号优先。ref_assets 的值可以是本地文件：提交前上传成媒资，指纹按文件内容算。

前置检查与幂等的做法和 video gen 相同：提示词要已 push；输入指纹没变不重复提交；
提交前落盘 submitting，被打断后重跑时先问远端。
"""

import hashlib
import json
import time

import httpx2

from wrally.api import shots as shots_api
from wrally.api.image import canvas_generate, submit_images
from wrally.commands import upload as upload_cmd
from wrally.commands.common import flow_seq, load_manifest, section, select
from wrally.commands.push import image_config
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
from wrally.errors import PreflightError, ToolError
from wrally.prompts import composed, load_styles
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

__all__ = ["HEADERS", "GenReport", "fingerprint", "gen"]


def fingerprint(prompt: str, config: dict, ref_assets: list[str]) -> str:
    payload = json.dumps([prompt, config, ref_assets], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _external_keyframes(kf: dict) -> str | None:
    if not kf.get("made_from") and (kf.get("candidates") or kf.get("asset_id")):
        return "has keyframes made outside wrally (--redo for more candidates)"
    return None


async def gen(
    ws: Workspace,
    client: McpClient,
    spec: str = "all",
    *,
    redo: bool = False,
    dry_run: bool = False,
    http: httpx2.AsyncClient | None = None,
) -> GenReport:
    data = load_manifest(ws)
    selected = select(data, spec)
    styles = load_styles(ws, data)
    config = image_config(ws.settings(data, "defaults"))
    series_id, project_id = data["series_id"], data["project_id"]
    by_key = {str(e["key"]): e for e in data["shots"]}
    report = GenReport(noun="keyframe")
    entries = skip_marked(selected, "keyframe", report)

    # —— 本地前置检查 ——
    prompts = {str(e["key"]): composed(ws, styles, e, "image") for e in entries}
    no_prompt = [e for e in entries if not (prompts[str(e["key"])] or "").strip()]
    if no_prompt:
        raise PreflightError("missing_image_prompt", f"({join_keys(no_prompt)})")
    named = ws.settings(data, "ref_assets")
    refs: dict[str, list[str]] = {}
    for e in entries:
        wanted = [str(r) for r in (e.get("keyframe") or {}).get("refs") or []]
        unknown = [r for r in wanted if r not in by_key and not named.get(r)]
        if unknown:
            raise PreflightError(
                "unknown_ref", f"({e['key']}: {','.join(unknown)} is neither a shot nor in ref_assets)"
            )
        assets = [
            ((by_key[r].get("keyframe") or {}).get("asset_id")) if r in by_key else str(named[r])
            for r in wanted
        ]
        if not all(assets):
            # 回顾镜头要用前面镜头的定稿关键帧作参考，没定稿就不能生成
            missing = [r for r, a in zip(wanted, assets, strict=True) if not a]
            raise PreflightError("missing_ref_pick", f"({e['key']} needs picks of {','.join(missing)})")
        refs[str(e["key"])] = assets
    prints = {
        k: fingerprint(prompts[k], config, [upload_cmd.print_of(ws, a) for a in refs[k]]) for k in prompts
    }

    todo, recover = classify(
        entries,
        prints,
        "keyframe",
        report,
        redo=redo,
        dry_run=dry_run,
        unchanged_suffix=" (--redo for more candidates)",
        external_note=_external_keyframes,
    )
    if dry_run or (not todo and not recover):
        return report

    # —— 远端前置检查与中断恢复 ——
    state = await inspect_and_recover(
        ws,
        client,
        data,
        entries,
        todo,
        recover,
        "keyframe",
        report,
        unrecoverable=lambda e: bool(refs[str(e["key"])]),
    )
    if not state.todo:
        return report
    remote, counts, todo = state.remote, state.counts, state.todo

    unpushed = [e for e in todo if remote[e["story_id"]].image_prompt.strip("\n") != prompts[str(e["key"])]]
    if unpushed:
        raise PreflightError("unpushed_prompts", f"({join_keys(unpushed)}; run: wrally push)")

    plain = [e for e in todo if not refs[str(e["key"])]]
    canvas = [e for e in todo if refs[str(e["key"])]]
    # 在网页上编辑过的镜头另有一份提示词，场次生图用的是那一份；画布任务的提示词是直接传的，不受影响
    stale_web = [e for e in plain if remote[e["story_id"]].rich_image_differs(prompts[str(e["key"])])]
    if stale_web:
        raise PreflightError(
            "stale_web_prompt", f"({join_keys(stale_web)}; paste the image prompt on the web)"
        )
    # ref_assets 里写成本地文件的先上传：失败了什么都还没提交
    values = [a for e in canvas for a in refs[str(e["key"])]]
    resolved = await upload_cmd.resolve(client, values, ws.root, project_id, http=http)
    for e in canvas:
        refs[str(e["key"])] = [str(resolved.ids[a]) for a in refs[str(e["key"])]]

    # —— 普通镜头：写配置 → 回读核对 ——
    if plain:
        await shots_api.set_model(
            client, series_id, project_id, [e["story_id"] for e in plain], "image", config,
            ws.raw_dir / "keyframe-set_model.json",
        )  # fmt: skip
        after = {
            s.story_id: s
            for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "keyframe-verify")
        }
        wrong = [
            e
            for e in plain
            if (after[e["story_id"]].gen_config.get("image") or {}).get("model") != config["model"]
        ]
        if wrong:
            raise PreflightError("config_not_applied", f"({join_keys(wrong)})")

    # —— 提交：先落盘 submitting，再发请求，成功后立刻记下任务 ID ——
    mark_submitting(ws, data, todo, "keyframe", prints, counts, task_key="tasks")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    if plain:
        raw = ws.raw_dir / "keyframe-submit.json"
        result = await submit_images(
            client, project_id, series_id, [e["story_id"] for e in plain], config["count"], raw
        )
        record_batch_submit(
            ws,
            data,
            plain,
            "keyframe",
            result,
            raw,
            report,
            stamp=stamp,
            task_key="tasks",
            detail=lambda _e, _t: f"{config['count']} images",
        )

    for e in canvas:
        key, kf = str(e["key"]), section(e, "keyframe")
        raw = ws.raw_dir / f"{key}-keyframe-canvas.json"
        try:
            tasks = await canvas_generate(
                client,
                prompt=prompts[key],
                model=config["model"],
                ref_asset_ids=refs[key],
                project_id=project_id,
                name=f"{data.get('title') or 'wrally'}_{key}",
                count=config["count"],
                aspect=config.get("aspectRatio"),
                resolution=config.get("resolution"),
                raw_path=raw,
                http=http,
            )
        except ToolError as err:
            kf["status"] = "failed"
            report.failed += 1
            report.rows.append((key, f"error: {err.code}", f"(raw: {raw})"))
        else:
            kf["status"] = "running"
            kf["submitted_at"] = stamp
            kf["canvas_tasks"] = flow_seq(tasks)
            report.submitted.append(key)
            report.rows.append(
                (key, "submitted", f"{len(tasks)} images via canvas, refs {','.join(kf['refs'])}")
            )
        ws.save(data)  # 每提交一镜就落盘，后面的镜头出错也不丢前面的任务 ID

    order = [str(e["key"]) for e in selected]
    report.submitted.sort(key=order.index)
    report.rows.sort(key=lambda r: (not r[1].startswith("error"), order.index(r[0])))
    return report
