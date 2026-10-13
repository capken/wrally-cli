import json
import stat
from types import SimpleNamespace

import pytest

from wrally.errors import ToolError
from wrally.transport.client import parse_result


def result(text, *, is_error=False, structured=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], structured_content=structured, is_error=is_error
    )


def test_parses_json_from_text_block():
    assert parse_result("t", result('{"head": {"code": 0}, "story_list": []}'))["story_list"] == []


def test_prefers_structured_content():
    assert parse_result("t", result("ignored", structured={"status": "success"})) == {"status": "success"}


def test_raw_is_written_before_raising_and_is_private(tmp_path):
    raw = tmp_path / "raw" / "10-video.json"
    with pytest.raises(ToolError) as e:
        parse_result("gen", result('{"head": {"code": 40012, "msg": "审核拒绝"}}'), raw)
    assert e.value.code == "40012" and e.value.raw_path == raw
    assert "审核拒绝" not in str(e.value)  # 终端只打错误码和路径
    assert stat.S_IMODE(raw.stat().st_mode) == 0o600
    assert "审核拒绝" in raw.read_text(encoding="utf-8")  # 中文不转义
    assert json.loads(raw.read_text(encoding="utf-8"))["tool"] == "gen"


def test_tool_error_flag_and_non_json():
    with pytest.raises(ToolError) as e:
        parse_result("t", result("something went wrong", is_error=True))
    assert e.value.code == "tool_error"
    with pytest.raises(ToolError) as e:
        parse_result("t", result("not json"))
    assert e.value.code == "bad_response"
