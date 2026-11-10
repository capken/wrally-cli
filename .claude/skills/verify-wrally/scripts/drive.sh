#!/usr/bin/env bash
# 在夹具里跑一条 wrally 命令并留下终端记录。
# 用法：drive.sh <运行目录> <记录名> <wrally 的参数…>
#   例：drive.sh "$RUN" status status -C "$RUN/project/ep1"
# 记录写到 <运行目录>/evidence/<记录名>.txt（命令、输出、退出码），同时打印到终端；脚本的退出码就是 wrally 的。
# 凭证目录指向 <运行目录>/cfg（空的）：离线命令照常能跑，调远端的会以退出码 3 失败，碰不到本机的凭证。
set -uo pipefail
. "$(dirname "$0")/env.sh"
RUN="${1%/}"
NAME="$2"
shift 2
OUT="$RUN/evidence/$NAME.txt"
mkdir -p "$RUN/evidence" "$RUN/cfg"
{
  echo "\$ wrally $*"
  WRALLY_CONFIG_DIR="$RUN/cfg" run_wrally "$@" 2>&1
  code=$?
  echo "[exit $code]"
  exit $code
} | tee "$OUT"
exit "${PIPESTATUS[0]}"
