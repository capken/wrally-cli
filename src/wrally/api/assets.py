"""媒资详情与下载。下载地址只在本模块内流转，调用方拿到的只有本地路径。"""

from dataclasses import dataclass
from pathlib import Path

import anyio
import httpx2
from pydantic import BaseModel, Field

from wrally.config import concurrency
from wrally.errors import NetworkError
from wrally.transport.client import RETRY_DELAYS, McpClient

BATCH = 50  # asset_detail 一次最多 50 个


class Asset(BaseModel):
    asset_id: str
    type: str = ""
    title: str = ""
    task_id: str = ""
    width: int = 0
    height: int = 0
    duration_ms: int = 0
    url: str = Field(default="", exclude=True, repr=False)  # 不参与序列化和打印

    @property
    def suffix(self) -> str:
        return Path(self.title).suffix.lower() or {"image": ".png", "video": ".mp4", "audio": ".wav"}.get(
            self.type, ""
        )


def _int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


async def asset_details(
    client: McpClient, asset_ids: list[str], raw_path: Path | None = None
) -> dict[str, Asset]:
    """批量取媒资详情。不存在的 ID 服务端会静默丢掉，返回的字典里就没有它。"""
    unique = list(dict.fromkeys(asset_ids))
    found: dict[str, Asset] = {}
    for n, start in enumerate(range(0, len(unique), BATCH)):
        data = await client.call(
            "asset_detail",
            {"asset_ids": unique[start : start + BATCH]},
            raw_path=raw_path
            if n == 0 or raw_path is None
            else raw_path.with_stem(f"{raw_path.stem}-{n + 1}"),
        )
        for a in data.get("assets") or []:
            det = a.get("asset_details") or {}
            found[a["id"]] = Asset(
                asset_id=a["id"],
                type=a.get("type") or "",
                title=a.get("title") or "",
                task_id=a.get("task_id") or "",
                width=_int(det.get("width")),
                height=_int(det.get("height")),
                duration_ms=_int(det.get("duration")),
                url=det.get("download_url") or det.get("url") or a.get("url") or "",
            )
    return found


async def plain_url(http: httpx2.AsyncClient, asset: Asset) -> str:
    """媒资的固定地址：asset_detail 给的是短链接，跳转后去掉查询串就是场次配置里存的那种地址。

    只读请求，连不上时按 RETRY_DELAYS 重试；仍然不行报 NetworkError（退出码 3），不当成意外错误。
    """
    for delay in (*RETRY_DELAYS, None):
        try:
            return await _follow(http, asset.url)
        except httpx2.TransportError as e:
            if delay is None:
                raise NetworkError(hint=f"(asset {asset.asset_id}: {type(e).__name__})") from e
            await anyio.sleep(delay)
    raise AssertionError("unreachable")


async def _follow(http: httpx2.AsyncClient, url: str) -> str:
    for _ in range(5):
        async with http.stream("GET", url) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = str(httpx2.URL(url).join(r.headers["location"]))
                continue
            break
    return url.split("?", 1)[0]


@dataclass
class Download:
    asset_id: str
    path: Path | None = None
    error: str = ""
    asset: Asset | None = None

    @property
    def ok(self) -> bool:
        return not self.error


class _Expired(Exception):
    pass


async def _fetch(http: httpx2.AsyncClient, url: str, dest: Path) -> str:
    """下载到 dest，返回错误码（空串表示成功）。网络错误和 5xx 退避重试；403 交给上层换地址。"""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            async with http.stream("GET", url, follow_redirects=True) as r:
                if r.status_code == 403:
                    raise _Expired
                if r.status_code >= 500:
                    raise httpx2.TransportError(f"HTTP {r.status_code}")
                if r.status_code != 200:
                    return f"http_{r.status_code}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")
                with tmp.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
                tmp.replace(dest)  # 中断不会留下看似完整的半个文件
                return ""
        except httpx2.TransportError:
            if attempt == len(RETRY_DELAYS):
                return "network_error"
            await anyio.sleep(RETRY_DELAYS[attempt])
    return "network_error"


async def download_assets(
    client: McpClient,
    items: list[tuple[str, Path]],
    *,
    raw_path: Path | None = None,
    http: httpx2.AsyncClient | None = None,
) -> list[Download]:
    """下载一批媒资。items 是 (asset_id, 不含扩展名的目标路径)，扩展名一律按媒资补上。

    不看目标路径「像不像有扩展名」：文件名里本来就可能有点（如 seed-audio-1.0），那样猜会漏补。

    一次批量取地址后并发下载；地址过期（403）时重新取一次地址再试。单个失败不影响其他。
    """
    if not items:
        return []
    details = await asset_details(client, [a for a, _ in items], raw_path)
    out = [Download(asset_id=a) for a, _ in items]
    limit = anyio.Semaphore(concurrency())
    owned = http is None
    http = http or httpx2.AsyncClient(timeout=httpx2.Timeout(30, read=120))

    async def one(i: int, asset_id: str, dest: Path) -> None:
        asset = details.get(asset_id)
        if asset is None or not asset.url:
            out[i].error = "asset_not_found"
            return
        dest = dest.with_name(dest.name + asset.suffix)
        async with limit:
            try:
                error = await _fetch(http, asset.url, dest)
            except _Expired:
                fresh = (await asset_details(client, [asset_id])).get(asset_id)
                try:
                    error = await _fetch(http, fresh.url, dest) if fresh and fresh.url else "asset_not_found"
                except _Expired:
                    error = "http_403"
        out[i] = Download(asset_id=asset_id, path=None if error else dest, error=error, asset=asset)

    try:
        async with anyio.create_task_group() as tg:
            for i, (asset_id, dest) in enumerate(items):
                tg.start_soon(one, i, asset_id, dest)
    finally:
        if owned:
            await http.aclose()
    return out
