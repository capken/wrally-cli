"""keyframe pick / sheet：选定关键帧、生成候选对比图。都只读写本地，不调用远端。

选哪张由人决定：先 sheet 出对比图，人指定后再 pick。
"""

from pathlib import Path

from wrally import media
from wrally.commands.common import letter, load_manifest, section, select
from wrally.errors import PreflightError
from wrally.workspace import Workspace

PICK_HEADERS = ("shot", "pick", "asset_id", "was", "note")
SHEET_HEADERS = ("shot", "candidates", "pick", "sheet")


def _index(choice: str) -> int | None:
    """候选序号 A、B、…、AA → 下标；不是序号（比如直接给了 asset_id）时返回 None。"""
    if not (choice.isalpha() and choice.isascii() and len(choice) <= 2):
        return None
    index = 0
    for ch in choice.upper():
        index = index * 26 + ord(ch) - 64
    return index - 1


def _label(candidates: list[str], asset_id: str | None) -> str:
    if not asset_id:
        return "-"
    return letter(candidates.index(asset_id)) if asset_id in candidates else asset_id


def pick(ws: Workspace, pairs: list[tuple[str, str]]) -> list[tuple[str, ...]]:
    """pairs 是 (镜号, 候选序号或 asset_id)。全部校验通过才写清单，有一个不对就什么都不改。"""
    data = load_manifest(ws)
    entries = {str(e["key"]): e for e in data["shots"]}
    resolved: list[tuple[str, str]] = []
    for raw_key, choice in pairs:
        key = raw_key.zfill(2) if raw_key.isdigit() else raw_key
        if key not in entries:
            raise PreflightError("unknown_shot", f"({key})")
        candidates = list((entries[key].get("keyframe") or {}).get("candidates") or [])
        index = _index(choice)
        if index is None:
            resolved.append((key, choice))
        elif not candidates:
            raise PreflightError("no_candidates", f"({key}; run: wrally fetch keyframe --shots {key})")
        elif index >= len(candidates):
            raise PreflightError(
                "unknown_candidate", f"({key} {choice.upper()}; have A-{letter(len(candidates) - 1)})"
            )
        else:
            resolved.append((key, candidates[index]))

    rows = []
    for key, asset_id in resolved:
        entry = entries[key]
        kf = section(entry, "keyframe")
        candidates = list(kf.get("candidates") or [])
        was = _label(candidates, kf.get("asset_id"))
        changed = kf.get("asset_id") != asset_id
        kf["asset_id"] = asset_id
        if asset_id in candidates:
            kf["pick"] = letter(candidates.index(asset_id))
        else:
            kf.pop("pick", None)
        made = (entry.get("video") or {}).get("status") == "done"
        note = "video was made with the previous pick" if changed and made and was != "-" else ""
        rows.append((key, _label(candidates, asset_id), asset_id, was, note))
    ws.save(data)
    return rows


def _files(directory: Path, stem: str) -> Path | None:
    return next(iter(sorted(directory.glob(f"{stem}.*"))), None)


def sheet(ws: Workspace, spec: str = "all") -> tuple[list[tuple[str, ...]], int]:
    """每镜一张候选对比图 .wrally/sheets/keyframes-<镜号>.jpg。返回 (表格行, 生成的张数)。"""
    data = load_manifest(ws)
    rows, made = [], 0
    for entry in select(data, spec):
        key = str(entry["key"])
        kf = entry.get("keyframe") or {}
        candidates = list(kf.get("candidates") or [])
        picked = kf.get("asset_id")
        directory = ws.state_dir / "keyframes" / key
        tiles = []
        for i, asset_id in enumerate(candidates):
            path = _files(directory, letter(i))
            if path:
                tiles.append((f"{key}-{letter(i)}", path, asset_id == picked))
        if picked and picked not in candidates:
            path = _files(directory, f"pick-{picked}")
            if path:
                tiles.append((f"{key}-pick", path, True))
        label = _label(candidates, picked) if picked in candidates or not picked else "file"
        if not tiles:
            hint = f"none (run: wrally fetch keyframe --shots {key})" if candidates or picked else "-"
            rows.append((key, str(len(candidates)), label, hint))
            continue
        out = ws.state_dir / "sheets" / f"keyframes-{key}.jpg"
        media.candidate_sheet(tiles, out)
        made += 1
        missing = len(candidates) - sum(1 for t in tiles if not t[0].endswith("-pick"))
        rows.append((key, f"{len(candidates)}" + (f" ({missing} not fetched)" if missing else ""), label,
                     out.relative_to(ws.root).as_posix()))  # fmt: skip
    return rows, made
