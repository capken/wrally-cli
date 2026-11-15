#!/usr/bin/env bash
# 用无界面 Chrome 看 wrally 生成的页面：截图，或者导出脚本跑完之后的 DOM。
# 用法：browser.sh shot <运行目录> <html 文件> <记录名> [宽,高]   → <运行目录>/evidence/<记录名>.png，再用 Read 看
#       browser.sh dom  <运行目录> <html 文件> <记录名>           → <运行目录>/evidence/<记录名>.html，并打印每张镜头卡片的开头
# 页面上的卡片、data-open（「未完成」筛选靠它）都是脚本画的，直接 grep page.html 看不到，要看导出的 DOM。
# Chrome 的位置默认是 macOS 的；别的系统设 CHROME。浏览器配置放在 <运行目录>/chrome-profile，cleanup 时删。
set -euo pipefail
MODE="$1"
RUN="${2%/}"
HTML="$(cd "$(dirname "$3")" && pwd)/$(basename "$3")"
NAME="$4"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
mkdir -p "$RUN/evidence"
chrome() {
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --user-data-dir="$RUN/chrome-profile" \
    --virtual-time-budget=3000 "$@" 2>/dev/null
}
case "$MODE" in
  shot)
    chrome --window-size="${5:-1400,1800}" --screenshot="$RUN/evidence/$NAME.png" "file://$HTML"
    echo "$RUN/evidence/$NAME.png"
    ;;
  dom)
    chrome --dump-dom "file://$HTML" > "$RUN/evidence/$NAME.html"
    grep -o '<section class="shot[^"]*" id="s[^"]*" data-open="[01]"' "$RUN/evidence/$NAME.html" || true
    echo "$RUN/evidence/$NAME.html"
    ;;
  *)
    echo "usage: browser.sh shot|dom <run> <html> <name> [w,h]" >&2
    exit 2
    ;;
esac
