import json

from typer.testing import CliRunner

from wrally import output
from wrally.cli import app
from wrally.transport import auth

HEADERS = ("shot", "status")


def test_table_stays_within_budget():
    rows = [(f"{i:02d}", "done") for i in range(40)]
    text = output.render("summary", HEADERS, rows, details=".wrally/pull.json")
    lines = text.splitlines()
    assert len(lines) <= 17  # 1 行结论 + 表头 + 不超过 15 行
    assert lines[-1] == "... 26 more  (details: .wrally/pull.json)"


def test_total_output_never_exceeds_30_lines():
    rows = [(f"{i:02d}", "done") for i in range(40)]
    assert len(output.render("s", HEADERS, rows, notes=[f"note {i}" for i in range(40)]).splitlines()) == 30


def test_columns_align_with_chinese():
    lines = output.render("s", ("shot", "remote", "x"), [("00", "片头", "a"), ("01", "01", "b")]).splitlines()
    # 按显示宽度对齐：一个汉字占两列
    assert output._width(lines[2][: lines[2].index("a")]) == output._width(lines[3][: lines[3].index("b")])


def test_json_keeps_chinese_unescaped_on_one_line():
    text = output.render_json("完成", HEADERS, [("片头", "done")], details="x")
    assert "\n" not in text and "\\u" not in text
    assert json.loads(text) == {
        "summary": "完成",
        "details": "x",
        "rows": [{"shot": "片头", "status": "done"}],
    }


def test_pull_without_series_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("WRALLY_CONFIG_DIR", str(tmp_path / "cfg"))
    result = CliRunner().invoke(app, ["pull", "-C", str(tmp_path)])
    assert result.exit_code == 2
    assert "error: missing_series" in result.output


def test_pull_when_not_logged_in_exits_3(tmp_path, monkeypatch):
    monkeypatch.setenv("WRALLY_CONFIG_DIR", str(tmp_path / "cfg"))
    result = CliRunner().invoke(app, ["pull", "-C", str(tmp_path), "--project", "p", "--series", "s"])
    assert result.exit_code == 3
    assert "error: not_logged_in  (run: wrally login)" in result.output
    assert not (tmp_path / "episode.yaml").exists()


def test_dump_public_has_no_tokens():
    creds = auth.Credentials(
        client_id="c", redirect_uri="r", issuer="i", authorization_endpoint="a", token_endpoint="t",
        resource="m", access_token="SECRET-AT", refresh_token="SECRET-RT", expires_at=0,
    )  # fmt: skip
    assert "SECRET" not in json.dumps(auth.dump_public(creds))
