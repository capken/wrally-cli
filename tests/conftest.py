import json

import pytest

SIGNED = "https://cdn.example.com/v/02.mp4?q-sign-algorithm=sha1&q-signature=abcdef"


def make_story(num: str, story_id: str, sort_num: int, *, keyframe: str | None = None) -> dict:
    """仿照 shotlist_manage list 的返回结构：嵌套 JSON 字符串、带签名地址。"""
    video = {"model": "73z6gomtng", "aspectRatio": "16:9", "duration": 8, "resolution": 1}
    if keyframe:
        video |= {
            "mode": "Text",
            "textModel": "73z6gomtng",
            "singleAssets": [
                {
                    "asset_id": keyframe,
                    "assetId": keyframe,
                    "fileType": "image",
                    "name": f"{num}.png",
                    "url": "https://cdn.example.com/i/k.png",
                }
            ],
        }
    extra = {
        "audio_prompt": f"旁白 {num}",
        "audio_role_data_json": json.dumps([{"asset_id": "voice1", "name": "旁白男声", "url": SIGNED}]),
        "gen_config": json.dumps({"image": {"model": "img1", "count": 1}, "video": video}),
    }
    return {
        "story_id": story_id,
        "story_num": num,
        "sort_num": sort_num,
        "image_prompt": f"画面 {num}「马赛克」",
        "animation_prompt": f"动画 {num}",
        "extra": json.dumps(extra, ensure_ascii=False),
        "story_audit_asset_detail": {"asset_id": f"vid{sort_num}", "url": SIGNED, "download_url": SIGNED},
    }


def set_web_copy(story: dict, text: str, field: str = "image_prompt_json") -> None:
    """模拟在网页上编辑过提示词：extra 里多出一份富文本副本，edit_content 不会动它。

    field 取 image_prompt_json 或 animation_prompt_json。
    """
    node = {
        "type": "text",
        "text": text,
        "detail": 0,
        "format": 0,
        "mode": "normal",
        "style": "",
        "version": 1,
    }
    doc = {"root": {"type": "root", "children": [{"type": "paragraph", "children": [node]}]}}
    extra = json.loads(story["extra"])
    extra[field] = json.dumps(doc, ensure_ascii=False)
    story["extra"] = json.dumps(extra, ensure_ascii=False)


@pytest.fixture
def stories() -> list[dict]:
    return [
        make_story("片头", "s00", 1),
        make_story("01", "s01", 2),
        make_story("02", "s02", 3, keyframe="key02"),
        make_story("片尾", "s99", 4),
    ]


# 画布模型目录：结构照真实返回（2026-10-21）裁剪
VIDEO_MODEL = {
    "id": "vid9",
    "name": "Seedance-2.0 Mini",
    "duration_options": [4, 8],
    "resolution_options": [{"value": "1"}],
    "support_audio": True,
}
CANVAS_MODELS = {
    "canvas_image_model_list": {
        "aspect_ratios": ["16:9", "1:1"],
        "models": [{"id": "img9", "name": "Seedream-5 Pro", "aspect_ratios": ["16:9", "1:1"],
                    "resolution_options": [{"value": "4", "label": "1K"}, {"value": "5", "label": "2K"}]}],
    },
    "canvas_video_provider_config": {
        "aspect_ratios": ["16:9", "9:16"],
        "modes": [
            {"mode": "Text", "models": [VIDEO_MODEL]},
            {"mode": "FirstLastFrame", "models": [VIDEO_MODEL]},
            {"mode": "SmartEdit", "models": [{"id": "edit1", "name": "Edit"}]},
        ],
    },
    "canvas_audio_model_list": {
        "audio_models": [
            {"model_id": "aud9", "name": "seed-audio-1.5", "is_minimax": False, "max_audio_count": 6,
             "restrictions": {"max_audio_count": 6}},
            {"model_id": "mm1", "name": "MiniMax-2.8-HD", "is_minimax": True, "restrictions": {}},
        ],
        "music_models": [{"model_id": "mus1", "name": "MiniMax-music3.0", "restrictions": {}}],
    },
}  # fmt: skip


class FakeWorkRally:
    """离线的 WorkRally：按工具名返回仿真结构，结果里照样带签名地址。"""

    def __init__(self, stories: list[dict]) -> None:
        self.stories = stories
        self.results: dict[tuple[str, str], dict] = {}
        self.assets: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.broken_stories: set[str] = set()
        self.http_hits: list[str] = []
        self.expired: set[str] = set()  # 这些资产的第一次下载返回 403
        self.submissions: list[list[str]] = []  # 每次被受理的生成请求
        self.canvas: dict[str, dict] = {}
        self.audio_edit_clobbers_config = False  # 模拟写旁白时把同字段里的模型配置冲掉
        self.canvas_requests: list[dict] = []
        self.submit_outcome = "ok"  # ok / timeout（已受理但响应丢了）/ lost（请求没到）
        self.server_skips: set[str] = set()
        self.series: list[dict] = []
        self.create_returns_id = True
        self.add_outcome = "ok"  # ok / timeout（已受理但响应丢了）

    def add_result(self, story_id: str, kind: str, asset_id: str, created: str, content: bytes = b"x",
                   title: str | None = None) -> None:  # fmt: skip
        ext = {"image": "png", "video": "mp4", "audio": "wav"}[kind]
        entry = self.results.setdefault((story_id, kind), {"doing": 0, "failed": 0, "results": []})
        entry["results"].append({"asset_id": asset_id, "asset_url": SIGNED, "created_at": created,
                                 "asset_title": title or f"shot.{ext}"})  # fmt: skip
        self.add_asset(asset_id, kind, content, title or f"shot.{ext}")

    def add_asset(self, asset_id: str, kind: str, content: bytes, title: str) -> None:
        self.assets[asset_id] = {"type": kind, "title": title, "task_id": f"task-{asset_id}"}
        self.files[asset_id] = content

    async def call(self, tool, arguments, *, raw_path=None, retry=True):
        from wrally.errors import ToolError

        self.calls.append((tool, arguments))
        if tool == "shotlist_manage":
            return self._manage(arguments, retry)
        if tool == "series_list":
            cover = "https://cdn.example.com/cover.png"
            return {"series_list": [{**s, "cover_image_url": cover} for s in self.series],
                    "page_info": {"total": len(self.series)}}  # fmt: skip
        if tool == "series_create":
            assert retry is False, "新建剧集不允许自动重试"
            made = {"series_id": f"ser{len(self.series) + 1}", "series_name": arguments["name"]}
            self.series.append(made)
            return {"series_id": made["series_id"]} if self.create_returns_id else {"message": "ok"}
        if tool == "shotlist_generate_video":
            return self._generate(arguments, retry, "video", 1)
        if tool == "shotlist_generate_audio":
            return self._generate(arguments, retry, "audio", 1)
        if tool == "shotlist_generate_image":
            return self._generate(arguments, retry, "image", arguments["count"])
        if tool in ("canvas_generate_image", "canvas_generate_video", "canvas_generate_audio"):
            from wrally.errors import NetworkError

            assert retry is False, "提交生成任务不允许自动重试"
            if self.submit_outcome == "lost":
                raise NetworkError(hint="generate TimeoutError")
            ids = [f"c{len(self.canvas) + i + 1}" for i in range(arguments["count"])]
            self.canvas.update({i: {"status": "running", "assets": []} for i in ids})
            self.canvas_requests.append(arguments)
            if self.submit_outcome == "timeout":
                raise NetworkError(hint="generate TimeoutError")
            return {"task_ids": ids}
        if tool in CANVAS_MODELS:
            return CANVAS_MODELS[tool]
        if tool == "canvas_get_task":
            return dict(self.canvas[arguments["task_id"]])
        if tool == "shotlist_model_list":
            return {"models": {"audio": [{"id": "aud1", "name": "seed-audio", "capabilities": [14]}]}}
        if tool == "shotlist_get_result":
            if arguments["story_id"] in self.broken_stories:
                raise ToolError("50001", tool=tool, raw_path=raw_path)
            entry = self.results.get((arguments["story_id"], arguments["result_type"]), {})
            results = sorted(entry.get("results", []), key=lambda r: r["created_at"], reverse=True)
            return {"doing_count": entry.get("doing", 0), "failed_count": entry.get("failed", 0),
                    "done_count": len(results), "results": results, "has_more": False}  # fmt: skip
        if tool == "asset_detail":
            return {"assets": [
                {"id": i, **self.assets[i], "asset_details": {"download_url": f"https://dl.example.com/{i}?sign=abc"}}
                for i in arguments["asset_ids"] if i in self.assets
            ]}  # fmt: skip
        raise AssertionError(f"unexpected tool {tool}")

    def _manage(self, arguments, retry=True):
        action = arguments["action"]
        by_id = {s["story_id"]: s for s in self.stories}
        if action == "list":
            return {"story_list": self.stories, "page_info": {"total": len(self.stories)}}
        if action == "add":
            from wrally.errors import NetworkError

            assert retry is False, "新建场次不允许自动重试"
            for shot in arguments["shots"]:
                assert shot.get("image_prompt") or shot.get("animation_prompt"), "场次至少带一种提示词"
                self.stories.append({
                    "story_id": f"new{len(self.stories) + 1}",
                    "story_num": shot["story_num"],
                    "sort_num": shot["sort_num"],
                    "image_prompt": shot.get("image_prompt", ""),
                    "animation_prompt": shot.get("animation_prompt", ""),
                    "extra": "",
                })  # fmt: skip
            if self.add_outcome == "timeout":
                raise NetworkError(hint="shotlist_manage TimeoutError")
            return {"message": "ok"}
        if action == "edit_content":
            assert arguments.get("series_id"), "edit_content 必须带 series_id"
            for edit in arguments["edits"]:
                story = by_id[edit["story_id"]]
                extra = json.loads(story["extra"])
                for name in ("image_prompt", "animation_prompt"):
                    if name in edit:
                        story[name] = edit[name]
                if "audio_prompt" in edit:
                    extra["audio_prompt"] = edit["audio_prompt"]
                    if self.audio_edit_clobbers_config:
                        extra["gen_config"] = json.dumps({})
                story["extra"] = json.dumps(extra, ensure_ascii=False)
            return {"message": "ok"}
        if action == "bind_asset":
            binding = arguments["binding"]
            assert binding["asset_type"] == "audio" and binding["mode"] == "replace"
            story = by_id[binding["story_id"]]
            extra = json.loads(story["extra"])
            extra["audio_role_data_json"] = json.dumps(binding["assets"], ensure_ascii=False)
            story["extra"] = json.dumps(extra, ensure_ascii=False)
            return {"message": "ok"}
        if action == "set_model":
            config = dict(arguments["model_config"])
            ids = config.pop("story_ids")
            assert ids, "不允许对全剧集 set_model"
            for story_id in ids:
                story = by_id[story_id]
                extra = json.loads(story["extra"])
                gen = json.loads(extra["gen_config"])
                gen.update(config)
                extra["gen_config"] = json.dumps(gen)
                story["extra"] = json.dumps(extra, ensure_ascii=False)
            return {"message": "ok"}
        raise AssertionError(f"unexpected action {action}")

    def _generate(self, arguments, retry, kind, per_shot):
        from wrally.errors import NetworkError

        assert retry is False, "提交生成任务不允许自动重试"
        accepted = [s for s in arguments["story_ids"] if s not in self.server_skips]
        if self.submit_outcome != "lost":
            for story_id in accepted:
                entry = self.results.setdefault((story_id, kind), {"doing": 0, "failed": 0, "results": []})
                entry["doing"] += per_shot
            self.submissions.append(accepted)
        if self.submit_outcome in ("lost", "timeout"):
            raise NetworkError(hint="generate TimeoutError")
        ids = [f"task-{s}" if per_shot == 1 else f"task-{s}-{n}" for s in accepted for n in range(per_shot)]
        return {"task_ids": ids, "skipped": [{"story_id": s} for s in self.server_skips]}

    def finish_image(self, story_id: str, asset_id: str, created: str, content: bytes = b"img"):
        self.results[(story_id, "image")]["doing"] -= 1
        self.add_result(story_id, "image", asset_id, created, content)

    def finish_canvas(
        self, task_id: str, asset_id: str | None, content: bytes = b"canvas", kind: str = "image"
    ) -> None:
        if asset_id is None:
            self.canvas[task_id] = {"status": "failed", "assets": []}
            return
        ext = {"image": "png", "video": "mp4", "audio": "wav"}[kind]
        self.add_asset(asset_id, kind, content, f"canvas.{ext}")
        self.canvas[task_id] = {"status": "success", "assets": [{"asset_id": asset_id}]}

    def finish(
        self, story_id: str, asset_id: str, created: str = "2026-10-06 12:00:00", failed: bool = False
    ):
        """让一个在跑的视频任务结束。"""
        entry = self.results[(story_id, "video")]
        entry["doing"] -= 1
        if failed:
            entry["failed"] += 1
        else:
            self.add_result(story_id, "video", asset_id, created)

    def actions(self) -> list[str]:
        """按顺序列出写操作，用来核对调用顺序。"""
        return [a["action"] for t, a in self.calls if t == "shotlist_manage" and a["action"] != "list"]

    def count(self, tool: str) -> int:
        return sum(1 for t, _ in self.calls if t == tool)

    def http(self):
        import httpx2

        def handler(request):
            asset_id = request.url.path.lstrip("/")
            self.http_hits.append(asset_id)
            if asset_id in self.expired:
                self.expired.discard(asset_id)
                return httpx2.Response(403)
            return httpx2.Response(200, content=self.files[asset_id])

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


@pytest.fixture
def remote(stories) -> FakeWorkRally:
    return FakeWorkRally(stories)


@pytest.fixture
def ws(tmp_path, stories):
    """已经 pull 过的工作区：00、01、02（已选用 key02）、99。"""
    from wrally.api.models import Shot
    from wrally.commands.pull import merge
    from wrally.workspace import Workspace

    workspace = Workspace(tmp_path)
    merge(workspace, [Shot.from_story(s) for s in stories], project_id="proj", series_id="ser")
    return workspace


def entry(ws, key: str) -> dict:
    return next(e for e in ws.load()["shots"] if e["key"] == key)
