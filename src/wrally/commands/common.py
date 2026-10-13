"""命令层共用：读清单、选镜头、并发执行并收集单镜错误。"""

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

import anyio
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from wrally.config import concurrency
from wrally.errors import AuthError, PreflightError, WrallyError
from wrally.workspace import Workspace, write_if_changed

T = TypeVar("T")


def load_manifest(ws: Workspace) -> CommentedMap:
    data = ws.load()
    if data is None or not data.get("shots"):
        raise PreflightError("no_manifest", "(run: wrally pull)")
    return data


WEB_COPIES = "web_copies.json"  # 在 .wrally/ 下


def remember_web_copies(ws: Workspace, shots: Iterable[Any]) -> None:
    """记下哪些场次在网页上编辑过提示词（远端另存着一份富文本副本）以及那份副本的文字。

    每次拿到完整的场次列表都整份重写。只有 page 读它：不调远端也能标出这些提示词要到网页上改。
    """
    found = {}
    for shot in shots:
        copies = {"image": shot.image_prompt_rich, "anim": shot.animation_prompt_rich}
        copies = {kind: text for kind, text in copies.items() if text is not None}
        if copies:
            found[shot.story_id] = copies
    text = json.dumps(found, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    write_if_changed(ws.state_dir / WEB_COPIES, text)


def web_copies(ws: Workspace) -> dict[str, dict[str, str]] | None:
    """story_id → {image / anim: 网页副本的文字}。还没有调过远端（文件不在）时返回 None。"""
    try:
        return json.loads((ws.state_dir / WEB_COPIES).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _norm(key: str) -> str:
    key = key.strip()
    return key.zfill(2) if key.isdigit() else key


def parse_shots(spec: str, keys: list[str]) -> list[str]:
    """--shots 支持 all、02,05、02-07；结果按清单顺序返回。"""
    if spec.strip() == "all":
        return list(keys)
    wanted: set[str] = set()
    for part in spec.split(","):
        if not part.strip():
            continue
        if "-" in part:
            lo, hi = (_norm(x) for x in part.split("-", 1))
            hit = [k for k in keys if lo <= k <= hi]
            if lo not in keys or hi not in keys or not hit:
                raise PreflightError("unknown_shot", f"({part.strip()})")
            wanted.update(hit)
        else:
            key = _norm(part)
            if key not in keys:
                raise PreflightError("unknown_shot", f"({key})")
            wanted.add(key)
    if not wanted:
        raise PreflightError("unknown_shot", f"({spec})")
    return [k for k in keys if k in wanted]


def select(data: CommentedMap, spec: str) -> list[CommentedMap]:
    entries = list(data["shots"])
    chosen = set(parse_shots(spec, [str(e["key"]) for e in entries]))
    return [e for e in entries if str(e["key"]) in chosen]


def section(entry: CommentedMap, name: str) -> CommentedMap:
    """取镜头下的 keyframe / video / audio 小节，没有就建一个。"""
    if not isinstance(entry.get(name), dict):
        entry[name] = CommentedMap()
    return entry[name]


def is_skipped(entry: CommentedMap, kind: str) -> bool:
    """清单里 `skip: [keyframe]` 表示这一镜不由 wrally 生成这类素材（比如片头沿用上一集的关键帧）。"""
    return kind in [str(k) for k in entry.get("skip") or []]


def flow_seq(items: list[Any]) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()  # 写成 [a, b] 一行，清单保持短小
    return seq


def letter(index: int) -> str:
    """候选序号：0 → A，25 → Z，26 → AA。"""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def error_cell(error: WrallyError) -> str:
    return f"error: {error.code}  {error.hint}".rstrip()


async def for_each(items: list[T], fn: Callable[[T], Awaitable[Any]]) -> list[Any]:
    """并发执行，单镜错误不中断其他镜头：对应位置返回 WrallyError。鉴权错误直接抛出。"""
    out: list[Any] = [None] * len(items)
    limit = anyio.Semaphore(concurrency())

    async def one(i: int, item: T) -> None:
        async with limit:
            try:
                out[i] = await fn(item)
            except AuthError:
                raise
            except WrallyError as e:
                out[i] = e

    try:
        async with anyio.create_task_group() as tg:
            for i, item in enumerate(items):
                tg.start_soon(one, i, item)
    except BaseExceptionGroup as group:
        auth = group.subgroup(AuthError)
        raise (auth.exceptions[0] if auth else group.exceptions[0]) from group
    return out
