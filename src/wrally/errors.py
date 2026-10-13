"""错误类型与退出码。

退出码：0 全部成功；1 部分镜头失败；2 前置检查不通过（未提交任何任务）；3 鉴权或网络错误。
错误信息只带错误码和提示，不带返回体。
"""

from pathlib import Path


class WrallyError(Exception):
    exit_code = 1
    code = "error"

    def __init__(self, code: str | None = None, hint: str = "") -> None:
        self.code = code or self.code
        self.hint = hint
        super().__init__(f"{self.code} {hint}".strip())


class PreflightError(WrallyError):
    exit_code = 2
    code = "preflight_failed"


class AuthError(WrallyError):
    exit_code = 3
    code = "auth_failed"


class NetworkError(WrallyError):
    exit_code = 3
    code = "network_error"


class ToolError(WrallyError):
    """WorkRally 返回的业务错误（审核拒绝、参数错误等），不自动重试。"""

    code = "tool_error"

    def __init__(self, code: str | None = None, *, tool: str = "", raw_path: Path | None = None) -> None:
        self.tool = tool
        self.raw_path = raw_path
        super().__init__(code, f"(raw: {raw_path})" if raw_path else "")
