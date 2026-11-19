"""三类场次生成命令（keyframe gen / video gen / audio gen）共用的幂等与提交骨架。

统一收口：过滤 skip → 按指纹分类（todo / recover）→ 拉取远端场次与基线并恢复 submitting
→ 提交前落盘 submitting → 批量提交回写状态与任务 ID。

各命令独有的前置检查、参数与远端配置写入保留在各自模块里。
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap

from wrally.api import shots as shots_api
from wrally.api.models import Shot
from wrally.api.results import ResultSet, get_results
from wrally.commands.common import flow_seq, for_each, is_skipped, remember_web_copies, section
from wrally.errors import PreflightError, WrallyError
from wrally.transport.client import McpClient
from wrally.workspace import Workspace

HEADERS = ("shot", "status", "detail")
RESULT_KINDS = {"keyframe": "image", "video": "video", "audio": "audio"}


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


@dataclass
class RemoteState:
    remote: dict[str, Shot]
    counts: dict[str, ResultSet]
    todo: list[CommentedMap]


def join_keys(entries: list[dict[str, Any]]) -> str:
    return ",".join(str(e["key"]) for e in entries)


def skip_marked(selected: list[CommentedMap], kind: str, report: GenReport) -> list[CommentedMap]:
    """把清单里标了 skip 的镜头记进 report，返回剩下的镜头。"""
    entries: list[CommentedMap] = []
    for e in selected:
        if is_skipped(e, kind):
            report.rows.append((str(e["key"]), "skipped", "marked skip in episode.yaml"))
        else:
            entries.append(e)
    return entries


def _default_external_note(sec: dict[str, Any]) -> str | None:
    if sec.get("status") == "done" and not sec.get("made_from"):
        return "done, made outside wrally (--redo to regenerate)"
    return None


def classify(
    entries: list[CommentedMap],
    prints: dict[str, str],
    kind: str,
    report: GenReport,
    *,
    redo: bool = False,
    dry_run: bool = False,
    redo_reason: Callable[[dict[str, Any]], str] | None = None,
    unchanged_suffix: str = "",
    external_note: Callable[[dict[str, Any]], str | None] = _default_external_note,
) -> tuple[list[CommentedMap], list[CommentedMap]]:
    """按清单状态和输入指纹把镜头分成待提交（todo）和待恢复（recover），其余跳过。

    dry_run 时同时把 would submit / would check 写进 report，不调用远端。
    """
    todo: list[CommentedMap] = []
    recover: list[CommentedMap] = []
    reasons: dict[str, str] = {}
    for e in entries:
        key = str(e["key"])
        sec = e.get(kind) or {}
        state, same = sec.get("status"), sec.get("made_from") == prints[key]
        why = redo_reason(e) if redo_reason else ""
        if why:
            reasons[key] = why
        if redo or why:
            todo.append(e)
        elif state == "submitting" and same:
            recover.append(e)  # 上次在提交途中被打断，先问远端
        elif state in ("running", "done") and same:
            report.rows.append((key, "skipped", f"{state}, inputs unchanged{unchanged_suffix}"))
        elif state == "running":
            report.rows.append((key, "skipped", "another task is running; wait for it first"))
        elif note := external_note(sec):
            report.rows.append((key, "skipped", note))
        else:
            todo.append(e)
    if dry_run:
        # 只做本地判断：列出会提交哪些镜头，不调用远端
        report.dry_run = True
        report.rows += [(str(e["key"]), "would submit", reasons.get(str(e["key"]), "")) for e in todo]
        report.rows += [(str(e["key"]), "would check", "interrupted run; remote decides") for e in recover]
    return todo, recover


async def inspect_and_recover(
    ws: Workspace,
    client: McpClient,
    data: CommentedMap,
    entries: list[CommentedMap],
    todo: list[CommentedMap],
    recover: list[CommentedMap],
    kind: str,
    report: GenReport,
    *,
    unrecoverable: Callable[[dict[str, Any]], bool] | None = None,
) -> RemoteState:
    """拉取完整远端场次列表和各镜结果基线，并对中断在 submitting 的镜头做恢复判断。"""
    series_id, project_id = data["series_id"], data["project_id"]
    remote = {
        s.story_id: s for s in await shots_api.shot_list(client, series_id, project_id, ws.raw_dir, kind)
    }
    remember_web_copies(ws, remote.values())
    active = todo + recover
    gone = [e for e in active if e["story_id"] not in remote]
    if gone:
        raise PreflightError("missing_on_remote", f"({join_keys(gone)})")

    result_kind = RESULT_KINDS[kind]

    async def count(entry: dict[str, Any]) -> ResultSet:
        return await get_results(
            client, entry["story_id"], result_kind, ws.raw_dir / f"{entry['key']}-{kind}-before.json"
        )

    raw_counts = dict(zip([str(e["key"]) for e in active], await for_each(active, count), strict=True))
    broken = [k for k, c in raw_counts.items() if isinstance(c, WrallyError)]
    if broken:
        raise PreflightError("cannot_read_results", f"({','.join(broken)})")
    counts: dict[str, ResultSet] = raw_counts

    pending = list(todo)
    for e in recover:
        key, sec = str(e["key"]), section(e, kind)
        rs = counts[key]
        if unrecoverable and unrecoverable(e):
            # 画布任务没有按镜头查询的入口，没法判断上次的请求是否已受理：不猜，交给人
            report.failed += 1
            report.rows.append((key, "error: unknown_submit_state", "check the web, then rerun with --redo"))
        elif rs.doing > 0 or rs.done > int(sec.get("results_before") or 0):
            sec["status"] = "running" if rs.doing > 0 else "done"
            report.rows.append((key, "skipped", "already submitted by an interrupted run"))
        else:
            pending.append(e)

    order = [str(x["key"]) for x in entries]
    pending.sort(key=lambda e: order.index(str(e["key"])))
    if not pending:
        ws.save(data)
    return RemoteState(remote=remote, counts=counts, todo=pending)


def mark_submitting(
    ws: Workspace,
    data: CommentedMap,
    entries: list[CommentedMap],
    kind: str,
    prints: dict[str, str],
    counts: dict[str, ResultSet],
    *,
    task_key: str = "task",
) -> None:
    """提交前把状态落盘为 submitting 并记下指纹和基线，保证进程随时被杀后可恢复。"""
    for e in entries:
        key, sec = str(e["key"]), section(e, kind)
        sec.pop(task_key, None)
        sec["status"] = "submitting"
        sec["made_from"] = prints[key]
        sec["results_before"] = counts[key].done
        sec["failed_before"] = counts[key].failed
    ws.save(data)


def record_batch_submit(
    ws: Workspace,
    data: CommentedMap,
    ready: list[CommentedMap],
    kind: str,
    result: dict[str, Any],
    raw: Path,
    report: GenReport,
    *,
    stamp: str | None = None,
    task_key: str = "task",
    detail: Callable[[dict[str, Any], Any], str] | None = None,
) -> None:
    """把场次批量提交接口的返回写回清单和报告，并立即落盘。"""
    skipped = json.dumps(result["skipped"], ensure_ascii=False)
    accepted = [e for e in ready if e["story_id"] not in skipped]
    ids = result["task_ids"]
    multi = task_key == "tasks"
    per_shot = len(ids) // len(accepted) if multi and accepted and len(ids) % len(accepted) == 0 else 0
    single_tasks = ids if (not multi and len(ids) == len(accepted)) else [None] * len(accepted)
    when = stamp or time.strftime("%Y-%m-%dT%H:%M:%S")
    for e in ready:
        key, sec = str(e["key"]), section(e, kind)
        if e not in accepted:
            sec["status"] = "failed"
            report.failed += 1
            report.rows.append((key, "error: skipped_by_server", f"(raw: {raw})"))
            continue
        sec["status"] = "running"
        sec["submitted_at"] = when
        i = accepted.index(e)
        task_val: Any = None
        if multi:
            if per_shot:
                task_val = ids[i * per_shot : (i + 1) * per_shot]
                sec[task_key] = flow_seq(task_val)
        else:
            task_val = single_tasks[i]
            if task_val:
                sec[task_key] = task_val
        report.submitted.append(key)
        text = detail(e, task_val) if detail else str(task_val or "")
        report.rows.append((key, "submitted", text))
    ws.save(data)
