"""运行期配置，全部可用环境变量覆盖。"""

import os
from pathlib import Path

CLIENT_NAME = "wrally"
SCOPE = "mcp"


def mcp_url() -> str:
    return os.environ.get("WRALLY_MCP_URL", "https://workrally.qq.com/zenstudio/api/mcp")


def config_dir() -> Path:
    """凭证目录，不在工作区里，不提交。"""
    override = os.environ.get("WRALLY_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".config" / "wrally"


def concurrency() -> int:
    """同一类调用的并发数。提交生成任务不受它控制，始终串行。"""
    try:
        return max(1, int(os.environ.get("WRALLY_CONCURRENCY", "4")))
    except ValueError:
        return 4
