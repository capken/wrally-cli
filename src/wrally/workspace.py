"""本地工作区：episode.yaml + prompts/ + .wrally/，一集的唯一状态来源。

上级目录里的 wrally.yaml 是项目文件：各集共用的 defaults、rules 写在那里，单集清单只写不同的部分。
wrally 不认识的小节也按同样的办法合并，由 export 原样带出。
"""

import io
import os
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from wrally.errors import PreflightError

MANIFEST = "episode.yaml"
PROJECT = "wrally.yaml"
# 项目文件里的路径相对项目文件所在目录；读进来时转成绝对路径，之后和单集里写的路径一样用
PROJECT_PATHS = (
    ("defaults", "style_suffix"),
    ("defaults", "anim_prefix"),
    ("defaults", "narration_template"),
)


def _yaml() -> YAML:
    yaml = YAML(typ="rt")  # round-trip：保留注释和键顺序
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def plain(value: Any) -> Any:
    """ruamel 读出来的带注释容器转成普通的 dict / list（也是一份深拷贝）。"""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def overlay(base: Any, over: Any) -> Any:
    """把 over 叠在 base 上：映射逐键合并，其余（列表、标量、null）整个替换。返回普通的 dict。"""
    if not isinstance(over, dict):
        return plain(over)
    out = plain(base) if isinstance(base, dict) else {}
    for key, value in over.items():
        out[str(key)] = overlay(out.get(str(key)), value)
    return out


def write_if_changed(path: Path, text: str) -> bool:
    """内容相同就不写，保证重跑不产生改动。原子替换，避免中断留下半个文件。"""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest_path = root / MANIFEST
        self.state_dir = root / ".wrally"
        self.raw_dir = self.state_dir / "raw"
        self._project: dict[str, Any] | None = None

    def project_path(self) -> Path | None:
        """离工作区最近的上级目录里的 wrally.yaml；没有就返回 None。"""
        for folder in self.root.resolve().parents:
            if (folder / PROJECT).is_file():
                return folder / PROJECT
        return None

    def project(self) -> dict[str, Any]:
        """项目文件的内容；没有项目文件时是空的。只读，wrally 不写它。"""
        if self._project is None:
            path = self.project_path()
            loaded = YAML(typ="safe").load(path.read_text(encoding="utf-8")) if path else None
            if loaded is not None and not isinstance(loaded, dict):
                raise PreflightError("bad_project_file", f"({path}: need a mapping)")
            self._project = loaded or {}
            for *parents, name in PROJECT_PATHS:
                node: Any = self._project
                for parent in parents:
                    node = node.get(parent) if isinstance(node, dict) else None
                if isinstance(node, dict) and isinstance(node.get(name), str):
                    node[name] = str(path.parent / Path(node[name]).expanduser())
        return self._project

    def settings(self, data: dict[str, Any] | None, name: str) -> dict[str, Any]:
        """某个共用小节的生效值：项目文件打底，单集清单覆盖。单集里写 null 表示这一集不要这一项。"""
        merged = overlay(self.project().get(name) or {}, (data or {}).get(name) or {})
        return merged if isinstance(merged, dict) else {}

    def output_path(self, data: dict[str, Any] | None) -> Path:
        """成片文件的位置：清单顶层的 output，缺省是 <目录名>.mp4。wrally 不做合成，只约定成片放在哪里。"""
        path = Path(str((data or {}).get("output") or f"{self.root.resolve().name}.mp4")).expanduser()
        return path if path.is_absolute() else self.root / path

    def load(self) -> CommentedMap | None:
        try:
            text = self.manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        data = _yaml().load(text)
        return data if data is not None else CommentedMap()

    def save(self, data: CommentedMap) -> bool:
        buf = io.StringIO()
        _yaml().dump(data, buf)
        return write_if_changed(self.manifest_path, buf.getvalue())

    def read_text(self, rel: str) -> str | None:
        try:
            return (self.root / rel).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write_text(self, rel: str, text: str) -> bool:
        return write_if_changed(self.root / rel, text)
