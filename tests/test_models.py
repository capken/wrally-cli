import pytest
from conftest import set_web_copy

from wrally.api import shots as shots_api
from wrally.api.models import Shot, shot_key
from wrally.errors import ToolError


@pytest.mark.parametrize(
    ("num", "key"), [("片头", "00"), ("片尾", "99"), ("02", "02"), ("7", "07"), ("11", "11")]
)
def test_shot_key_maps_intro_and_outro(num, key):
    assert shot_key(num) == key


def test_from_story_parses_nested_json(stories):
    shot = Shot.from_story(stories[2])
    assert shot.key == "02"
    assert shot.image_prompt == "画面 02「马赛克」"
    assert shot.audio_prompt == "旁白 02"
    assert shot.keyframe_asset_id == "key02"
    assert shot.video_asset_id == "vid3"
    assert shot.voice_asset_ids == ["voice1"]
    assert shot.gen_config["video"]["mode"] == "Text"


def test_no_url_leaves_the_api_layer(stories):
    """签名 URL 只允许存在于接口层和下载器里。"""
    for story in stories:
        assert "http" not in Shot.from_story(story).model_dump_json()


def test_web_copy_of_image_prompt_is_read_as_plain_text(stories):
    assert Shot.from_story(stories[1]).image_prompt_rich is None
    set_web_copy(stories[1], "网页上的旧文本")
    shot = Shot.from_story(stories[1])
    assert shot.image_prompt_rich == "网页上的旧文本"
    assert shot.rich_image_differs(shot.image_prompt) and not shot.rich_image_differs("网页上的旧文本\n")
    # 字段在但没有文字（网页建过又清空）：当作没有这一份
    set_web_copy(stories[1], "")
    assert Shot.from_story(stories[1]).image_prompt_rich is None


def test_shot_without_single_assets_has_no_keyframe(stories):
    assert Shot.from_story(stories[1]).keyframe_asset_id is None


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def call(self, tool, arguments, *, raw_path=None, retry=True):
        self.calls.append((tool, arguments, raw_path))
        return self.pages[arguments["page"] - 1]


async def test_shot_list_uses_list_and_follows_pages(stories, tmp_path):
    pages = [
        {"story_list": stories[2:], "page_info": {"total": 4}},
        {"story_list": stories[:2], "page_info": {"total": 4}},
    ]
    client = FakeClient(pages)
    result = await shots_api.shot_list(client, "ser", "proj", raw_dir=tmp_path)
    assert [s.key for s in result] == ["00", "01", "02", "99"]  # 按 sort_num 排序
    assert all(tool == "shotlist_manage" and args["action"] == "list" for tool, args, _ in client.calls)
    assert [raw.name for _, _, raw in client.calls] == ["pull-list-p1.json", "pull-list-p2.json"]


def test_api_does_not_expose_get():
    """shotlist_manage get 返回空提示词，接口层不暴露它。"""
    assert not [name for name in dir(shots_api) if "get" in name.lower()]


def test_tool_error_message_has_no_body(tmp_path):
    err = ToolError("audit_rejected", tool="x", raw_path=tmp_path / "02-video.json")
    assert str(err) == f"audit_rejected (raw: {tmp_path / '02-video.json'})"


def test_web_copy_of_anim_prompt_is_read_as_plain_text(stories):
    assert Shot.from_story(stories[1]).animation_prompt_rich is None
    set_web_copy(stories[1], "网页上的旧动画", "animation_prompt_json")
    shot = Shot.from_story(stories[1])
    assert shot.animation_prompt_rich == "网页上的旧动画" and shot.image_prompt_rich is None
    assert shot.rich_anim_differs(shot.animation_prompt) and not shot.rich_anim_differs("网页上的旧动画\n")
