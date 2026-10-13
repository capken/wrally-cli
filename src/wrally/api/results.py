"""场次的生成结果与进度（shotlist_get_result）。

结果里只有 asset_id，没有任务 ID；视频和音频的结果带签名地址，这里解析时全部丢弃。
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from wrally.transport.client import McpClient

Kind = Literal["image", "video", "audio"]


class Result(BaseModel):
    asset_id: str
    title: str = ""
    created_at: str = ""


class ResultSet(BaseModel):
    kind: Kind
    doing: int = 0
    done: int = 0
    failed: int = 0
    results: list[Result] = Field(default_factory=list)  # 按生成时间从旧到新

    @property
    def state(self) -> str:
        """running / done / failed / none。failed 只在一个结果都没有时才算，历史失败不影响已有结果。"""
        if self.doing > 0:
            return "running"
        if self.results:
            return "done"
        return "failed" if self.failed > 0 else "none"

    @property
    def latest(self) -> Result | None:
        return self.results[-1] if self.results else None


async def get_results(
    client: McpClient, story_id: str, kind: Kind, raw_path: Path | None = None
) -> ResultSet:
    results: list[Result] = []
    page = 1
    while True:
        data = await client.call(
            "shotlist_get_result",
            {"story_id": story_id, "result_type": kind, "page": page, "page_size": 20},
            raw_path=raw_path
            if page == 1 or raw_path is None
            else raw_path.with_stem(f"{raw_path.stem}-p{page}"),
        )
        results.extend(
            Result(
                asset_id=r["asset_id"], title=r.get("asset_title") or "", created_at=r.get("created_at") or ""
            )
            for r in data.get("results") or []
            if r.get("asset_id")
        )
        if page == 1:
            counts = {k: int(data.get(f"{k}_count") or 0) for k in ("doing", "done", "failed")}
        if not data.get("has_more") or not data.get("results"):
            break
        page += 1
    results.sort(key=lambda r: r.created_at)
    return ResultSet(kind=kind, results=results, **counts)
