"""场次接口。

只暴露 list：shotlist_manage 的 get 返回空提示词，容易让人误以为没写入，所以不包装它。
"""

from pathlib import Path

from wrally.api.models import Shot
from wrally.transport.client import McpClient

PAGE_SIZE = 50


async def shot_list(
    client: McpClient, series_id: str, project_id: str, raw_dir: Path | None = None, label: str = "pull"
) -> list[Shot]:
    """拉取剧集下的全部场次，按 sort_num 排序。原始返回按页写入 raw_dir。"""
    shots: list[Shot] = []
    page = 1
    while True:
        data = await client.call(
            "shotlist_manage",
            {
                "action": "list",
                "series_id": series_id,
                "project_id": project_id,
                "page": page,
                "page_size": PAGE_SIZE,
            },
            raw_path=raw_dir / f"{label}-list-p{page}.json" if raw_dir else None,
        )
        stories = data.get("story_list") or []
        shots.extend(Shot.from_story(s) for s in stories)
        total = int((data.get("page_info") or {}).get("total") or 0)
        if not stories or len(shots) >= total:
            break
        page += 1
    return sorted(shots, key=lambda s: s.sort_num)


async def add_shots(
    client: McpClient, series_id: str, project_id: str, shots: list[dict], raw_path: Path | None = None
) -> None:
    """批量新建场次。每条是 {story_num, sort_num, image_prompt}，服务端要求至少带一种提示词。

    不自动重试：超时后无法判断服务端是否已受理，重发会建出重复的场次。新场次的 ID 用 list 取。
    """
    await client.call(
        "shotlist_manage",
        {"action": "add", "series_id": series_id, "project_id": project_id, "shots": shots},
        raw_path=raw_path,
        retry=False,
    )


EDIT_BATCH = 20


async def edit_content(
    client: McpClient, series_id: str, project_id: str, edits: list[dict], raw_dir: Path | None = None
) -> None:
    """批量写提示词。每条是 {story_id, image_prompt?, animation_prompt?, audio_prompt?}。

    必须在 set_model 之前调用。始终带上 series_id：写 audio_prompt 时服务端要靠它读取并合并 extra，
    否则可能把同在 extra 里的模型配置冲掉。
    """
    for n, start in enumerate(range(0, len(edits), EDIT_BATCH)):
        await client.call(
            "shotlist_manage",
            {
                "action": "edit_content",
                "series_id": series_id,
                "project_id": project_id,
                "edits": edits[start : start + EDIT_BATCH],
            },
            raw_path=raw_dir / f"push-edit_content-{n + 1}.json" if raw_dir else None,
        )


async def set_model(
    client: McpClient,
    series_id: str,
    project_id: str,
    story_ids: list[str],
    partition: str,
    config: dict,
    raw_path: Path | None = None,
) -> None:
    """把同一份配置写入若干场次的一个分区（image / video / audio）。story_ids 不能为空：空表示全剧集。"""
    if not story_ids:
        raise ValueError("set_model 需要明确的 story_ids")
    await client.call(
        "shotlist_manage",
        {
            "action": "set_model",
            "series_id": series_id,
            "project_id": project_id,
            "model_config": {"story_ids": story_ids, partition: config},
        },
        raw_path=raw_path,
    )


async def model_capabilities(
    client: McpClient, category: str, model_id: str, raw_path: Path | None = None
) -> list:
    """模型的 capabilities，写音频配置时要带上（服务端用它区分 text_to_audio 模型）。"""
    data = await client.call("shotlist_model_list", {"categories": [category]}, raw_path=raw_path)

    def find(node: object) -> list | None:
        if isinstance(node, dict):
            if node.get("id") == model_id:
                return list(node.get("capabilities") or [])
            node = list(node.values())
        if isinstance(node, list):
            for child in node:
                found = find(child)
                if found is not None:
                    return found
        return None

    found = find(data.get("models"))
    if found is None:
        from wrally.errors import PreflightError

        raise PreflightError("unknown_model", f"({category}: {model_id})")
    return found
