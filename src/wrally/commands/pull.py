"""pull：从 WorkRally 拉取场次列表，生成或更新 episode.yaml 与 prompts/。

本地优先：清单里已有的字段不覆盖；本地提示词与远端不同时保留本地（视为待 push 的改动），
用 --force 才以远端为准。远端是 `new` 写的占位文本时，即使 --force 也不覆盖本地。重跑不产生任何改动。

条目按 story_id 对应远端场次；还没有 story_id 的手写条目按镜号认领，补上 story_id，其余字段不动。
"""

import json
from collections import Counter
from dataclasses import asdict, dataclass, field

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from wrally.api.models import Shot
from wrally.commands.common import remember_web_copies
from wrally.errors import PreflightError
from wrally.prompts import FILE_FIELD, PLACEHOLDER, Styles, composed, load_styles, local_text, set_raw, split
from wrally.workspace import Workspace, write_if_changed

# (提示词类别, Shot 属性)
PROMPT_KINDS = (("image", "image_prompt"), ("anim", "animation_prompt"), ("audio", "audio_prompt"))
HEADERS = ("shot", "remote", "image", "anim", "audio", "keyframe", "note")


@dataclass
class ShotRow:
    shot: str
    remote: str
    image: str = "-"
    anim: str = "-"
    audio: str = "-"
    keyframe: str = "-"
    note: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.note) or any(
            s in ("new", "updated", "local") for s in (self.image, self.anim, self.audio)
        )


@dataclass
class PullReport:
    rows: list[ShotRow] = field(default_factory=list)
    new_shots: int = 0
    prompts_written: int = 0
    kept_local: int = 0
    notes: list[str] = field(default_factory=list)

    def table(self) -> list[tuple[str, ...]]:
        # 有变化的行排前面，表格被截断时先看到它们
        ordered = sorted(self.rows, key=lambda r: not r.changed)
        return [tuple(asdict(r).values()) for r in ordered]


def _adopt(ws: Workspace, styles: Styles, entry: CommentedMap, kind: str, remote: str) -> None:
    """以远端为准写入本地：能拆出公共文本就只存独有部分，拆不出就存完整文本并标 raw。"""
    key = str(entry["key"])
    got = split(styles, entry, kind, remote)
    if got is not None:
        body, border = got
        set_raw(entry, kind, False)
        if border and border != (entry.get("border") or styles.default_border):
            entry["border"] = border
        if kind == "audio":
            entry.pop(FILE_FIELD[kind], None)
            entry["narration"] = body
            return
    else:
        body = remote.strip("\n")
        set_raw(entry, kind, styles.template(kind) is not None)
    rel = str(entry.get(FILE_FIELD[kind]) or f"prompts/{key}.{kind}.md")
    ws.write_text(rel, body + "\n")
    entry[FILE_FIELD[kind]] = rel


def _sync_prompt(
    ws: Workspace, styles: Styles, entry: CommentedMap, kind: str, remote: str, force: bool
) -> str:
    """返回 new / same / updated / local / -。"""
    rel = f"prompts/{entry['key']}.{kind}.md"
    if (
        local_text(ws, entry, kind) is None
        and not entry.get(FILE_FIELD[kind])
        and ws.read_text(rel) is not None
    ):
        # 清单没引用、但默认位置已有文件（手写的）：当作本地文本，不拿远端覆盖
        entry[FILE_FIELD[kind]] = rel
    try:
        local = composed(ws, styles, entry, kind)
    except PreflightError:
        local = ""  # 本地文本拼不出来（比如已含公共文本），按有差异处理
    if not remote.strip():
        return "-" if local is None else "local"
    if local is not None:
        if local.strip("\n") == remote.strip("\n"):
            return "same"
        if not force or remote.strip() == PLACEHOLDER:
            return "local"
    _adopt(ws, styles, entry, kind, remote)
    return "new" if local is None else "updated"


def check_manifest_keys(entries: list) -> None:
    """清单里同一个镜号或同一个 story_id 只能有一个条目。"""
    keys = Counter(str(e.get("key")) for e in entries)
    ids = Counter(e.get("story_id") for e in entries if e.get("story_id"))
    dup = sorted([k for k, n in keys.items() if n > 1] + [str(i) for i, n in ids.items() if n > 1])
    if dup:
        raise PreflightError("duplicate_shot_key", f"({','.join(dup)} in episode.yaml)")


def merge(
    ws: Workspace,
    shots: list[Shot],
    *,
    project_id: str,
    series_id: str,
    title: str | None = None,
    force: bool = False,
) -> PullReport:
    data = ws.load()
    if data is None:
        data = CommentedMap()
    for name, value in (("project_id", project_id), ("series_id", series_id)):
        if data.get(name) not in (None, value):
            raise PreflightError("series_mismatch", f"({name} in episode.yaml differs)")
        data.setdefault(name, value)
    if title and "title" not in data:
        data["title"] = title
    if data.get("shots") is None:
        data["shots"] = CommentedSeq()
    entries = data["shots"]
    styles = load_styles(ws, data)
    check_manifest_keys(entries)
    by_story = {e["story_id"]: e for e in entries if e.get("story_id")}
    by_key = {str(e.get("key")): e for e in entries}

    dup = [k for k, n in Counter(s.key for s in shots if s.story_id not in by_story).items() if n > 1]
    if dup:
        raise PreflightError("duplicate_shot_key", f"({','.join(sorted(dup))})")
    # 先把冲突查完再动文件，出错时不留下半截状态
    claims: dict[str, CommentedMap] = {}
    for shot in shots:
        owner = by_key.get(shot.key)
        if shot.story_id in by_story or owner is None:
            continue
        if owner.get("story_id"):
            raise PreflightError("duplicate_shot_key", f"({shot.key})")
        claims[shot.story_id] = owner

    report = PullReport()
    stale_web: list[str] = []
    stale_web_anim: list[str] = []
    for shot in shots:
        entry = by_story.get(shot.story_id)
        row = ShotRow(shot="", remote=shot.story_num)
        if entry is None and shot.story_id in claims:
            entry = claims[shot.story_id]
            if "story_id" in entry:  # 手写了空的 story_id
                entry["story_id"] = shot.story_id
            else:
                entry.insert(list(entry).index("key") + 1, "story_id", shot.story_id)
            row.note = "claimed"
        elif entry is None:
            entry = CommentedMap(key=DoubleQuotedScalarString(shot.key), story_id=shot.story_id)
            entries.append(entry)
            report.new_shots += 1
            row.note = "new shot"
        if row.note and shot.keyframe_asset_id and "keyframe" not in entry:
            # 只在首次建档时记下远端当前选用的关键帧，之后以清单为准
            entry["keyframe"] = CommentedMap(asset_id=shot.keyframe_asset_id)
        key = str(entry["key"])
        row.shot = key
        for kind, attr in PROMPT_KINDS:
            state = _sync_prompt(ws, styles, entry, kind, getattr(shot, attr), force)
            setattr(row, kind, state)
            report.prompts_written += state in ("new", "updated")
            report.kept_local += state == "local"
        if shot.rich_image_differs(shot.image_prompt):
            row.note = "; ".join(filter(None, (row.note, "web copy differs")))
            stale_web.append(key)
        if shot.rich_anim_differs(shot.animation_prompt):
            row.note = "; ".join(filter(None, (row.note, "web anim copy differs")))
            stale_web_anim.append(key)
        row.keyframe = str((entry.get("keyframe") or {}).get("asset_id") or "-")
        report.rows.append(row)

    remote_ids = {s.story_id for s in shots}
    for entry in entries:
        if entry.get("story_id") not in remote_ids:
            report.rows.append(ShotRow(shot=str(entry.get("key")), remote="-", note="missing on remote"))

    if not ws.settings(data, "defaults"):
        report.notes.append(
            "note: no `defaults` yet in episode.yaml or wrally.yaml (models, style_suffix, anim_prefix)"
        )
    if report.kept_local:
        report.notes.append(
            "note: `local` = local prompt differs from remote, kept (push it, or pull --force)"
        )
    if stale_web:
        report.notes.append(
            "note: `web copy differs` = the web editor keeps its own copy of the image prompt, image "
            "generation uses that copy, and push cannot update it; paste the prompt on the web"
        )
    if stale_web_anim:
        report.notes.append(
            "note: `web anim copy differs` = same for the anim prompt: video generation uses the web "
            "editor's copy, and push cannot update it; paste the anim prompt on the web"
        )
    ws.save(data)
    remember_web_copies(ws, shots)
    write_if_changed(
        ws.state_dir / "pull.json",
        json.dumps([asdict(r) for r in report.rows], ensure_ascii=False, indent=1) + "\n",
    )
    return report


def summary(report: PullReport) -> str:
    return (
        f"pulled {len([r for r in report.rows if r.remote != '-'])} shots: {report.new_shots} new, "
        f"{report.prompts_written} prompts written, {report.kept_local} kept local  -> episode.yaml"
    )
