"""本地媒体处理：图像比对、抽帧拼图、时长、旁白音高。不涉及远端。"""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

from wrally.errors import PreflightError

SAME_IMAGE_MSE = 200.0  # 同一张图重新上传或转码后差异在几十以内，不同的图在几千


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PreflightError(f"{name}_missing", f"(install {name})")
    return path


def image_distance(a: Path, b: Path, size: int = 64) -> float:
    """两张图缩到同一尺寸后的逐像素均方差。"""
    from PIL import Image

    def pixels(path: Path) -> bytes:
        with Image.open(path) as im:
            return im.convert("RGB").resize((size, size), Image.Resampling.BILINEAR).tobytes()

    pa, pb = pixels(a), pixels(b)
    return sum((x - y) ** 2 for x, y in zip(pa, pb, strict=True)) / len(pa)


def same_image(a: Path, b: Path) -> bool:
    return image_distance(a, b) < SAME_IMAGE_MSE


def duration(path: Path) -> float:
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (ValueError, KeyError):
        return 0.0


def contact_sheet(video: Path, out: Path, *, columns: int = 3, rows: int = 2, width: int = 320) -> bool:
    """从视频里均匀抽 columns×rows 帧，拼成一张小图。"""
    length = duration(video)
    if length <= 0:
        return False
    n = columns * rows
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-frames:v", "1", "-q:v", "5",
         "-vf", f"fps={n / length:.6f},scale={width}:-2,tile={columns}x{rows}", str(out)],
        capture_output=True, check=False,
    )  # fmt: skip
    return done.returncode == 0 and out.exists()


PITCH_HINT = "install wrally with the `pitch` extra"


def pitch_available() -> bool:
    """量音高要用的 librosa 装了没有。它是可选依赖（pitch），很重，只有设了 rules.f0_max 的片子用得到。"""
    return importlib.util.find_spec("librosa") is not None


def channel_gap(audio: Path) -> float | None:
    """各声道 RMS 电平（dB）最大值和最小值之差；单声道是 0，量不出来返回 None。
    生成的旁白偶尔只有一个声道有声音（《提灯人》EP6 的 02：左 -19 dB、右 -61 dB），
    音高和时长都正常，只能这样查。"""
    out = subprocess.run(
        [_tool("ffmpeg"), "-v", "info", "-nostats", "-i", str(audio),
         "-af", "astats=measure_overall=none:measure_perchannel=RMS_level", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    levels = []
    for line in out.stderr.splitlines():
        if "RMS level dB:" in line:
            value = line.rsplit(":", 1)[1].strip()
            try:
                levels.append(max(float(value), -120.0))  # 完全无声是 -inf
            except ValueError:
                return None
    return round(max(levels) - min(levels), 1) if levels else None


def median_f0(audio: Path) -> float | None:
    """旁白的基频中位数（Hz）。rules.f0_max 是按这组参数定的，换参数会让同一条旁白差出几十赫兹。"""
    import librosa  # 导入很慢，只在 check 里用到时才加载
    import numpy as np

    samples, rate = librosa.load(str(audio), sr=16000)
    if samples.size == 0:
        return None
    f0, _, _ = librosa.pyin(samples, fmin=55, fmax=300, sr=rate, frame_length=1024)
    voiced = f0[~np.isnan(f0)]
    return float(np.median(voiced)) if voiced.size else None


def candidate_sheet(
    tiles: list[tuple[str, Path, bool]], out: Path, *, columns: int = 3, width: int = 480
) -> None:
    """把候选关键帧拼成一张对比图。tiles 是 (标签, 图片路径, 是否已选)；已选的加绿色粗框。"""
    from PIL import Image, ImageDraw, ImageFont

    columns = max(1, min(columns, len(tiles)))
    with Image.open(tiles[0][1]) as first:
        height = round(width * first.height / first.width)
    gap = 8
    rows = -(-len(tiles) // columns)
    sheet = Image.new(
        "RGB", (columns * width + (columns + 1) * gap, rows * height + (rows + 1) * gap), (24, 24, 24)
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=30)
    for i, (label, path, picked) in enumerate(tiles):
        x = gap + (i % columns) * (width + gap)
        y = gap + (i // columns) * (height + gap)
        with Image.open(path) as im:
            sheet.paste(im.convert("RGB").resize((width, height), Image.Resampling.LANCZOS), (x, y))
        if picked:
            draw.rectangle((x - 5, y - 5, x + width + 4, y + height + 4), outline=(40, 200, 80), width=6)
        text = f"{label}  PICKED" if picked else label
        box = draw.textbbox((x + 10, y + 8), text, font=font)
        draw.rectangle(
            (box[0] - 8, box[1] - 6, box[2] + 8, box[3] + 6), fill=(40, 200, 80) if picked else (0, 0, 0)
        )
        draw.text((x + 10, y + 8), text, font=font, fill=(0, 0, 0) if picked else (255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=82)
