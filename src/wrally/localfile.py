"""写成本地路径的媒资：判断、定位、按内容算指纹。不调远端，工作区和接口层都用它。

参考图、首尾帧、音色既可以写媒资 ID，也可以写本地文件。媒资 ID 只有字母和数字，
所以值里带扩展名、斜杠或 ~ 的算路径。
"""

import hashlib
from pathlib import Path

from wrally.errors import PreflightError

# upload_file 说明里列出的媒体格式
TYPES = {
    **dict.fromkeys(("png", "jpg", "jpeg", "webp", "gif", "bmp"), "image"),
    **dict.fromkeys(("mp4", "mov", "avi", "mkv", "webm"), "video"),
    **dict.fromkeys(("mp3", "wav", "aac", "flac", "ogg", "m4a"), "audio"),
}


def is_local(value: object) -> bool:
    """这个值写的是本地文件而不是媒资 ID。"""
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    return "/" in value or "\\" in value or value.startswith("~") or Path(value).suffix.lower() != ""


def local_path(value: str, base: Path) -> Path:
    """写在清单或参数里的本地路径 → 绝对路径；相对路径相对 base。"""
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else base / path


def check_file(path: Path) -> str:
    """上传前的本地检查，返回媒资类型。文件不在或格式不支持时报前置错误。"""
    if not path.is_file():
        raise PreflightError("missing_file", f"({path})")
    kind = TYPES.get(path.suffix.lower().lstrip("."))
    if kind is None:
        raise PreflightError("unsupported_file", f"({path.name}; allowed: {', '.join(TYPES)})")
    return kind


def sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_print(path: Path) -> str:
    """进输入指纹的写法：按内容，不按路径和媒资 ID。文件换了内容才算输入变了。"""
    return f"file:{sha1(path)}"
