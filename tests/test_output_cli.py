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


def test_json_error_goes_to_stdout_as_one_json_line(tmp_path):
    result = CliRunner().invoke(app, ["status", "--json", "-C", str(tmp_path)])
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"error": "no_manifest", "hint": "(run: wrally pull)", "exit_code": 2}
    assert "error: no_manifest" in result.stderr


def test_unexpected_error_is_one_line_exit_4_with_traceback_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WRALLY_CONFIG_DIR", str(tmp_path / "cfg"))

    def boom(ws):
        raise ValueError("bad https://x.test/a?sig=1")

    monkeypatch.setattr("wrally.cli.status_cmd.status", boom)
    (tmp_path / "episode.yaml").write_text("shots: []\n")
    result = CliRunner().invoke(app, ["status", "--json", "-C", str(tmp_path)])
    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"] == "internal_error"
    assert "Traceback" not in result.output
    crash = (tmp_path / "cfg" / "last-crash.txt").read_text()
    assert "ValueError" in crash and "sig=1" not in crash


def test_wrally_error_inside_exception_group_keeps_its_exit_code(tmp_path, monkeypatch):
    from wrally.errors import AuthError

    def boom(ws):
        raise ExceptionGroup("tg", [AuthError("login_expired", "(run: wrally login)")])

    monkeypatch.setattr("wrally.cli.status_cmd.status", boom)
    (tmp_path / "episode.yaml").write_text("shots: []\n")
    result = CliRunner().invoke(app, ["status", "-C", str(tmp_path)])
    assert result.exit_code == 3 and "error: login_expired" in result.stderr


def test_max_wait_caps_every_timeout(monkeypatch):
    from wrally import cli

    monkeypatch.setenv("WRALLY_MAX_WAIT", "90")
    assert cli._limit(1200) == 90 and cli._limit(30) == 30
    monkeypatch.setenv("WRALLY_MAX_WAIT", "abc")
    assert cli._limit(1200) == 1200


def test_gen_accepts_wait_as_the_default(monkeypatch):
    """gen 默认就等待并下载；--wait 不报错、效果同默认，--no-wait 只提交。"""
    from wrally import cli

    seen = []
    monkeypatch.setattr(cli, "_gen", lambda kind, params, out, project, no_wait, *rest: seen.append(no_wait))
    for flags in ([], ["--wait"], ["--no-wait"]):
        result = CliRunner().invoke(app, ["gen", "video", "推镜", "--model", "m", *flags])
        assert result.exit_code == 0, result.output
    assert seen == [False, False, True]
