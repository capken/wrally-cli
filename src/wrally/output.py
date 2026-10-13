"""终端输出：一行结论 + 不超过 15 行的表，总长不超过 30 行；明细写文件。

任何模式都不输出签名 URL、提示词全文和返回体。中文直接以 UTF-8 输出。
"""

import json
import unicodedata
from collections.abc import Sequence
from typing import Any

MAX_TABLE_ROWS = 15
MAX_LINES = 30


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def render(
    summary: str,
    headers: Sequence[str] = (),
    rows: Sequence[Sequence[Any]] = (),
    *,
    details: str | None = None,
    notes: Sequence[str] = (),
) -> str:
    """rows 超出预算时只显示前面的行，其余折叠成一行并指向明细文件。调用方负责把要紧的行排在前面。"""
    lines = [summary]
    cells = [[str(c) for c in row] for row in rows]
    if cells:
        hidden = 0
        if len(cells) > MAX_TABLE_ROWS:
            hidden = len(cells) - (MAX_TABLE_ROWS - 1)
            cells = cells[: MAX_TABLE_ROWS - 1]
        widths = [max(_width(h), *(_width(r[i]) for r in cells)) for i, h in enumerate(headers)]
        lines.append("  ".join(_pad(h, w) for h, w in zip(headers, widths, strict=True)).rstrip())
        lines.extend("  ".join(_pad(c, w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells)
        if hidden:
            lines.append(f"... {hidden} more" + (f"  (details: {details})" if details else ""))
    lines.extend(notes)
    return "\n".join(lines[:MAX_LINES])


def render_json(
    summary: str, headers: Sequence[str] = (), rows: Sequence[Sequence[Any]] = (), **extra: Any
) -> str:
    payload = {"summary": summary, **extra, "rows": [dict(zip(headers, r, strict=True)) for r in rows]}
    return json.dumps(payload, ensure_ascii=False)
