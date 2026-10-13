"""push：把本地提示词写回 WorkRally。

顺序固定：edit_content 批量写入 → 对受影响的镜头重写 set_model，否则生成时会报「未配置模型」。
只写与远端不同的镜头；全部一致时不发任何写请求。
"""

import shutil
import time
from dataclasses import dataclass, field

from wrally.api import shots as shots_api
from wrally.commands.common import load_manifest, remember_web_copies, select
from wrally.errors import PreflightError
from wrally.prompts import composed, load_styles
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

# --what → (提示词类别, 远端字段)
WHAT = {
    "prompts": (("image", "image_prompt"), ("anim", "animation_prompt")),
    "audio-prompt": (("audio", "audio_prompt"),),
}


@dataclass
class PushReport:
    what: str
    dry_run: bool
    headers: tuple[str, ...] = ()
    rows: list[tuple[str, ...]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    shots: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)
    backup: str = ""

    @property
    def summary(self) -> str:
        detail = ", ".join(f"{k} {n}" for k, n in self.counts.items())
        verb = "dry run: would push" if self.dry_run else "pushed"
        return f"{verb} {self.what}: {self.shots} shots ({detail}), {self.failed} failed"


def image_config(defaults: dict) -> dict:
    image = defaults.get("image") or {}
    if not image.get("model"):
        raise PreflightError("missing_defaults", "(defaults.image.model)")
    config = {"model": image["model"], "count": int(image.get("count") or 1)}
    if image.get("aspect"):
        config["aspectRatio"] = image["aspect"]
    if image.get("resolution") is not None:
        config["resolution"] = image["resolution"]
    return config


async def push(
    ws: Workspace, client: McpClient, what: str, spec: str = "all", dry_run: bool = False
) -> PushReport:
    if what not in WHAT:
        raise PreflightError("unknown_what", f"({what}; use prompts or audio-prompt)")
    kinds = WHAT[what]
    data = load_manifest(ws)
    entries = select(data, spec)
    styles = load_styles(ws, data)
    defaults = ws.settings(data, "defaults")
    # 先把本地要发的文本全部拼好：拼不出来就在写任何东西之前退出
    wanted = {str(e["key"]): {kind: composed(ws, styles, e, kind) for kind, _ in kinds} for e in entries}
    report = PushReport(what, dry_run, headers=("shot", *(kind for kind, _ in kinds)))
    report.counts = {kind: 0 for kind, _ in kinds}

    series_id, project_id = data["series_id"], data["project_id"]
    remote = {
        s.story_id: s for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "push")
    }
    remember_web_copies(ws, remote.values())
    edits: list[dict] = []
    touched: dict[str, list[str]] = {kind: [] for kind, _ in kinds}  # 类别 → story_id
    stale_web: list[str] = []  # 网页那份图片提示词与本地不同的镜头；edit_content 改不到那一份
    stale_web_anim: list[str] = []  # 动画提示词同理
    for entry in entries:
        key = str(entry["key"])
        shot = remote.get(entry["story_id"])
        if shot is None:
            report.failed += 1
            report.rows.append((key, "error: missing on remote", *[""] * (len(kinds) - 1)))
            continue
        fields, cells = {}, []
        for kind, remote_field in kinds:
            text = wanted[key][kind]
            if kind == "image" and text is not None and shot.rich_image_differs(text):
                stale_web.append(key)
            if kind == "anim" and text is not None and shot.rich_anim_differs(text):
                stale_web_anim.append(key)
            if text is None:
                cells.append("-")
            elif text.strip("\n") == getattr(shot, remote_field).strip("\n"):
                cells.append("same")
            else:
                fields[remote_field] = text
                touched[kind].append(shot.story_id)
                report.counts[kind] += 1
                cells.append("push")
        if fields:
            edits.append({"story_id": shot.story_id, **fields})
        report.rows.append((key, *cells))
    report.shots = len(edits)
    report.rows.sort(key=lambda r: "push" not in r and not r[1].startswith("error"))
    if stale_web:
        report.notes.append(
            f"note: {','.join(stale_web)}: the web editor's copy of the image prompt differs and push "
            "cannot update it; paste the prompt on the web before `keyframe gen`"
        )
    if stale_web_anim:
        report.notes.append(
            f"note: {','.join(stale_web_anim)}: the web editor's copy of the anim prompt differs and push "
            "cannot update it; paste the anim prompt on the web before `video gen`"
        )

    # 配置也在写之前备好，缺默认值时不留下「写了提示词却没重写配置」的半截状态
    configs: dict[str, dict] = {}
    if touched.get("image"):
        configs["image"] = image_config(defaults)
    if touched.get("audio"):
        model = (defaults.get("audio") or {}).get("model")
        if not model:
            raise PreflightError("missing_defaults", "(defaults.audio.model)")
        caps = [] if dry_run else await shots_api.model_capabilities(client, "audio", model)
        configs["audio"] = {"model": model, "count": 1, "modelCapabilities": caps}
    if dry_run or not edits:
        return report

    # 写之前把远端原文另存一份：这是覆盖前唯一的底稿，不能被下一次运行冲掉
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for page in sorted(ws.raw_dir.glob("push-list-p*.json")):
        backup = ws.raw_dir / "history" / f"push-{stamp}-before-{page.name.removeprefix('push-list-')}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(page, backup)
        report.backup = backup.parent.relative_to(ws.root).as_posix() + f"/push-{stamp}-before-*.json"

    await shots_api.edit_content(client, series_id, project_id, edits, ws.raw_dir)
    for partition, config in configs.items():
        await shots_api.set_model(
            client, series_id, project_id, touched[partition], partition, config,
            ws.raw_dir / f"push-set_model-{partition}.json",
        )  # fmt: skip

    # 回读核对：提示词确实写进去了，视频配置没有被这次写入碰坏
    after = {
        s.story_id: s
        for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, "push-verify")
    }
    remember_web_copies(ws, after.values())
    by_story = {e["story_id"]: str(e["key"]) for e in entries}
    for edit in edits:
        key, now, before = by_story[edit["story_id"]], after.get(edit["story_id"]), remote[edit["story_id"]]
        problems = []
        if now is None or any(getattr(now, f).strip("\n") != text.strip("\n")
                              for f, text in edit.items() if f != "story_id"):  # fmt: skip
            problems.append("not_applied")
        elif now.gen_config.get("video") != before.gen_config.get("video"):
            problems.append("video_config_changed")
        if problems:
            report.failed += 1
            report.rows = [(key, f"error: {problems[0]}", *r[2:]) if r[0] == key else r for r in report.rows]
    if report.backup:
        report.notes.append(f"backup of remote before this push: {report.backup}")
    if "anim" in touched and touched["anim"]:
        report.notes.append("note: video config is rewritten by `video gen`, not by push")
    return report
