"""命令行入口。只有这一层与人或 Agent 交互。"""

import typer

from wrally.cli import canvas, episode, media, web
from wrally.cli.base import _emit, _limit, _remote, handle_errors, refreshes_page
from wrally.cli.canvas import _gen
from wrally.commands import status as status_cmd

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="WorkRally 批量命令行工具",
)

episode.register_core(app)
media.register(app)
episode.register_checks(app)
web.register(app)
canvas.register(app)

__all__ = [
    "_emit",
    "_gen",
    "_limit",
    "_remote",
    "app",
    "handle_errors",
    "refreshes_page",
    "status_cmd",
]
