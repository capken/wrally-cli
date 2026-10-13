"""keyframe gen：生成关键帧候选。

普通镜头：set_model image → shotlist_generate_image（全部镜头一次提交）。
带 refs 的镜头：用被引用镜头的定稿关键帧作参考图，走画布图生图，逐镜提交。

前置检查与幂等的做法和 video gen 相同：提示词要已 push；输入指纹没变不重复提交；
提交前落盘 submitting，被打断后重跑时先问远端。
"""

import hashlib
import json
import time

import httpx2

from wrally.api import shots as shots_api
from wrally.api.image import canvas_generate, submit_images
from wrally.api.results import get_results
from wrally.commands.common import (
    flow_seq,
    for_each,
    is_skipped,
    load_manifest,
    remember_web_copies,
    section,
    select,
)
from wrally.commands.push import image_config
from wrally.commands.video import GenReport
from wrally.errors import PreflightError, ToolError, WrallyError
from wrally.prompts import composed, load_styles
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("shot", "status", "detail")


def fingerprint(prompt: str, config: dict, ref_assets: list[str]) -> str:
    payload = json.dumps([prompt, config, ref_assets], ensure_ascii=False, sort_keys=True)
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
    http: httpx2.AsyncClient | None = None,
) -> GenReport:
    data = load_manifest(ws)
    selected = select(data, spec)
    styles = load_styles(ws, data)
    config = image_config(ws.settings(data, "defaults"))
    series_id, project_id = data["series_id"], data["project_id"]
    by_key = {str(e["key"]): e for e in data["shots"]}
    report = GenReport(noun="keyframe")
    for e in [e for e in selected if is_skipped(e, "keyframe")]:
        report.rows.append((str(e["key"]), "skipped", "marked skip in episode.yaml"))
    entries = [e for e in selected if not is_skipped(e, "keyframe")]

    # —— 本地前置检查 ——
    prompts = {str(e["key"]): composed(ws, styles, e, "image") for e in entries}
    no_prompt = [e for e in entries if not (prompts[str(e["key"])] or "").strip()]
    if no_prompt:
        raise PreflightError("missing_image_prompt", f"({_keys(no_prompt)})")
    refs: dict[str, list[str]] = {}
    for e in entries:
        wanted = [str(r) for r in (e.get("keyframe") or {}).get("refs") or []]
        assets = [((by_key.get(r) or {}).get("keyframe") or {}).get("asset_id") for r in wanted]
        if not all(assets):
            # 回顾镜头要用前面镜头的定稿关键帧作参考，没定稿就不能生成
            missing = [r for r, a in zip(wanted, assets, strict=True) if not a]
            raise PreflightError("missing_ref_pick", f"({e['key']} needs picks of {','.join(missing)})")
        refs[str(e["key"])] = assets
    prints = {k: fingerprint(prompts[k], config, refs[k]) for k in prompts}

    todo, recover = [], []
    for e in entries:
        key, kf = str(e["key"]), e.get("keyframe") or {}
        state, same = kf.get("status"), kf.get("made_from") == prints[key]
        if redo:
            todo.append(e)
        elif state == "submitting" and same:
            recover.append(e)
        elif state in ("running", "done") and same:
            report.rows.append((key, "skipped", f"{state}, inputs unchanged (--redo for more candidates)"))
        elif state == "running":
            report.rows.append((key, "skipped", "another task is running; wait for it first"))
        elif not kf.get("made_from") and (kf.get("candidates") or kf.get("asset_id")):
            report.rows.append(
                (key, "skipped", "has keyframes made outside wrally (--redo for more candidates)")
            )
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
        s.story_id: s
        for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "keyframe")
    }
    remember_web_copies(ws, remote.values())
    gone = [e for e in todo + recover if e["story_id"] not in remote]
    if gone:
        raise PreflightError("missing_on_remote", f"({_keys(gone)})")

    async def count(entry: dict):
        return await get_results(
            client, entry["story_id"], "image", ws.raw_dir / f"{entry['key']}-keyframe-before.json"
        )

    counts = dict(
        zip([str(e["key"]) for e in todo + recover], await for_each(todo + recover, count), strict=True)
    )
    broken = [k for k, c in counts.items() if isinstance(c, WrallyError)]
    if broken:
        raise PreflightError("cannot_read_results", f"({','.join(broken)})")
    for e in recover:
        key, kf = str(e["key"]), section(e, "keyframe")
        rs = counts[key]
        if refs[key]:
            # 画布任务没有按镜头查询的入口，没法判断上次的请求是否已受理：不猜，交给人
            report.failed += 1
            report.rows.append((key, "error: unknown_submit_state", "check the web, then rerun with --redo"))
        elif rs.doing > 0 or rs.done > int(kf.get("results_before") or 0):
            kf["status"] = "running" if rs.doing > 0 else "done"
            report.rows.append((key, "skipped", "already submitted by an interrupted run"))
        else:
            todo.append(e)
    todo.sort(key=lambda e: [str(x["key"]) for x in entries].index(str(e["key"])))
    if not todo:
        ws.save(data)
        return report
    unpushed = [e for e in todo if remote[e["story_id"]].image_prompt.strip("\n") != prompts[str(e["key"])]]
    if unpushed:
        raise PreflightError("unpushed_prompts", f"({_keys(unpushed)}; run: wrally push)")

    plain = [e for e in todo if not refs[str(e["key"])]]
    canvas = [e for e in todo if refs[str(e["key"])]]
    # 在网页上编辑过的镜头另有一份提示词，场次生图用的是那一份；画布任务的提示词是直接传的，不受影响
    stale_web = [e for e in plain if remote[e["story_id"]].rich_image_differs(prompts[str(e["key"])])]
    if stale_web:
        raise PreflightError("stale_web_prompt", f"({_keys(stale_web)}; paste the image prompt on the web)")

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
            raise PreflightError("config_not_applied", f"({_keys(wrong)})")

    # —— 提交：先落盘 submitting，再发请求，成功后立刻记下任务 ID ——
    for e in todo:
        key, kf = str(e["key"]), section(e, "keyframe")
        kf.pop("tasks", None)
        kf["status"] = "submitting"
        kf["made_from"] = prints[key]
        kf["results_before"] = counts[key].done
        kf["failed_before"] = counts[key].failed
    ws.save(data)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    if plain:
        raw = ws.raw_dir / "keyframe-submit.json"
        result = await submit_images(
            client, project_id, series_id, [e["story_id"] for e in plain], config["count"], raw
        )
        skipped = json.dumps(result["skipped"], ensure_ascii=False)
        accepted = [e for e in plain if e["story_id"] not in skipped]
        ids = result["task_ids"]
        per_shot = len(ids) // len(accepted) if accepted and len(ids) % len(accepted) == 0 else 0
        for e in plain:
            key, kf = str(e["key"]), section(e, "keyframe")
            if e not in accepted:
                kf["status"] = "failed"
                report.failed += 1
                report.rows.append((key, "error: skipped_by_server", f"(raw: {raw})"))
                continue
            kf["status"] = "running"
            kf["submitted_at"] = stamp
            if per_shot:
                i = accepted.index(e)
                kf["tasks"] = flow_seq(ids[i * per_shot : (i + 1) * per_shot])
            report.submitted.append(key)
            report.rows.append((key, "submitted", f"{config['count']} images"))
        ws.save(data)

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
