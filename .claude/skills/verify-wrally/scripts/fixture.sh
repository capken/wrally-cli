#!/usr/bin/env bash
# 建一个离线的验证用创作项目：项目文件 + 一集三镜 + ffmpeg 合成的素材。打印这次运行的目录。
# 用法：fixture.sh [运行目录]      缺省是 ${TMPDIR:-/tmp}/wrally-verify/<时间戳>
# 目录里：project/（工作区，cleanup 时删）、cfg/（空的凭证目录）、evidence/（证据，保留）
set -euo pipefail
RUN="${1:-${TMPDIR:-/tmp}/wrally-verify/$(date +%Y%m%d-%H%M%S)}"
RUN="${RUN%/}"
P="$RUN/project"
if [ -e "$P" ]; then
  echo "already exists: $P" >&2
  exit 2
fi
mkdir -p "$P/styles" "$P/ep1/prompts" "$P/ep1/.wrally/video" "$P/ep1/.wrally/audio" \
  "$P/ep1/.wrally/keyframes/01" "$P/ep1/.wrally/keyframes/02" "$RUN/cfg" "$RUN/evidence"

cat > "$P/wrally.yaml" <<'YAML'
# 验证用的项目文件（verify-wrally 的夹具，不对应任何一部片子，ID 都是假的，不能调远端）
project_id: fixture-project
title: 验证夹具
defaults:
  style_suffix: styles/suffix.md
  anim_prefix: styles/prefix.md
rules:
  slow_max: 1.25
  narration_max: 12
  words:
    fine motion: {kind: anim, words: [写字]}
assemble:            # wrally 不认识的小节：export 原样带进 extra
  bgm: assets/bgm.wav
YAML
printf '画风：水彩。\n' > "$P/styles/suffix.md"
printf '镜头固定。\n' > "$P/styles/prefix.md"

cat > "$P/ep1/episode.yaml" <<'YAML'
project_id: fixture-project
series_id: fixture-series
title: 第一集
shots:
  - key: "01"          # 三类素材齐全，已选图
    story_id: fx01
    image_prompt: prompts/01.image.md
    anim_prompt: prompts/01.anim.md
    narration: 一盏灯亮了。
    keyframe:
      asset_id: kf01a
      candidates: [kf01a, kf01b]
    video: {asset_id: v01, status: done, file: .wrally/video/01.mp4}
    audio: {asset_id: a01, status: done, file: .wrally/audio/01.wav}
  - key: "02"          # 旁白 6 秒配 4 秒视频（放慢 1.5 倍）、台词超长、动画提示词里有词表里的词、还没选图
    story_id: fx02
    image_prompt: prompts/02.image.md
    anim_prompt: prompts/02.anim.md
    narration: 他提着灯走过很长很长的一条走廊。
    cue: {note: 原样带出}
    keyframe:
      candidates: [kf02a, kf02b]
    video: {asset_id: v02, status: done, file: .wrally/video/02.mp4}
    audio: {asset_id: a02, status: done, file: .wrally/audio/02.wav}
  - key: "03"          # 只有提示词：没有视频、旁白和台词
    story_id: fx03
    image_prompt: prompts/03.image.md
    anim_prompt: prompts/03.anim.md
YAML
printf '一盏油灯放在木桌上\n' > "$P/ep1/prompts/01.image.md"
printf '灯焰轻轻摇晃。\n' > "$P/ep1/prompts/01.anim.md"
printf '一个人提灯走在走廊里\n' > "$P/ep1/prompts/02.image.md"
printf '他停下来在墙上写字。\n' > "$P/ep1/prompts/02.anim.md"
printf '走廊尽头的一扇门\n' > "$P/ep1/prompts/03.image.md"
printf '门缓缓打开。\n' > "$P/ep1/prompts/03.anim.md"

ff() { ffmpeg -hide_banner -loglevel error -y "$@"; }
for n in 01 02; do
  ff -f lavfi -i "testsrc=duration=4:size=320x180:rate=24" -pix_fmt yuv420p "$P/ep1/.wrally/video/$n.mp4"
done
ff -f lavfi -i "sine=frequency=110:duration=3" -ac 2 "$P/ep1/.wrally/audio/01.wav"
ff -f lavfi -i "sine=frequency=110:duration=6" -ac 2 "$P/ep1/.wrally/audio/02.wav"
ff -f lavfi -i "color=c=red:size=320x180" -frames:v 1 "$P/ep1/.wrally/keyframes/01/A.png"
ff -f lavfi -i "color=c=green:size=320x180" -frames:v 1 "$P/ep1/.wrally/keyframes/01/B.png"
ff -f lavfi -i "color=c=blue:size=320x180" -frames:v 1 "$P/ep1/.wrally/keyframes/02/A.png"
ff -f lavfi -i "color=c=orange:size=320x180" -frames:v 1 "$P/ep1/.wrally/keyframes/02/B.png"
echo "$RUN"
