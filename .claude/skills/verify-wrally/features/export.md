# 导出

`wrally export` 把合成脚本需要的输入写成 `.wrally/export.json`：每镜用哪个视频、哪段旁白、台词和时长，缺什么、什么过期了；清单和项目文件里 wrally 不认识的键原样放进 `extra`。不调远端，不做合成。

## Sub-features

- `export-shots` 每镜的 `video`、`audio`（相对单集目录的路径）、`video_seconds`、`audio_seconds`、`narration`。
- `export-missing` 缺视频、旁白或台词的镜头列在终端，并写进这一镜的 `missing`。
- `export-extra` 项目文件和清单里不认识的键进 `extra`，每镜不认识的键进这一镜的 `extra`。
- `export-project-dir` 有项目文件时 `project_dir` 是到项目目录的相对路径。
- `export-idempotent` 输入没变，重跑报 `unchanged`，文件不变。
- `export-subset` `--shots` 只导出几镜。

## How to get to it (user POV)

- 在单集目录里敲 `wrally export`，或者 `wrally export -C <单集目录>`。
- 合成脚本读 `<单集目录>/.wrally/export.json`。

## Driving it with drive.sh

Preconditions:

- 基线状态的夹具，`E="$RUN/project/ep1"`。

- **导出。** `export -C "$E"`。退出码 0；摘要行 `exported 3 shots, 1 with missing or stale material  -> .wrally/export.json  (updated)`；一行 `03  video,audio,narration  -`。
- **看文件。** 读 `$E/.wrally/export.json`。`version` 是 1，`title` 是 `第一集`，`output` 是 `ep1.mp4`，`project_dir` 是 `..`；`shots[0]` 的 `video` 是 `.wrally/video/01.mp4`、`video_seconds` 4.0、`audio_seconds` 3.0；`shots[1].extra` 是 `{"cue": {"note": "原样带出"}}`；`shots[2].missing` 是 `["video", "audio", "narration"]`；顶层 `extra` 是 `{"assemble": {"bgm": "assets/bgm.wav"}}`。
- **重跑。** 先记下 `shasum "$E/.wrally/export.json"`，再 `export -C "$E"`。摘要行以 `(unchanged)` 结尾，校验和不变。
- **只导出一镜。** `export --shots 01 -C "$E"`。摘要行 `exported 1 shots  -> .wrally/export.json  (updated)`，没有表；文件里只剩 `01`。
- **Proof.** 把 `$E/.wrally/export.json` 复制到 `$RUN/evidence/`（做过「只导出一镜」的话，先不带 `--shots` 再导一次）。

## Gotchas

- `--shots` 会用子集覆盖同一个 `export.json`，不是另写一个文件。验证完记得整集再导一次，否则后面读它的步骤只看到一镜。
- `extra` 里的路径（夹具里的 `assets/bgm.wav`）wrally 不解析，那个文件在夹具里并不存在，这不是错误。
- 夹具的素材都没有 `made_from`，所以 `stale` 永远是空的。要验证过期标记，需要 wrally 生成过的素材，也就是真实项目，或者手写一个对不上的 `made_from`（这时还要在 `wrally.yaml` 里补 `defaults.video` 或 `defaults.audio`）。
- 有超分版时 `video` 指向超分文件、`video_upscale` 是倍数；夹具里没有超分，这一项恒为 `null`。
- 加了新的清单字段却没补进 `commands/export.py` 的 `TOP_KEYS`、`SHOT_KEYS`，它会出现在 `extra` 里。改过清单字段就在夹具的清单里写上这个字段，确认它没有被带出去。
