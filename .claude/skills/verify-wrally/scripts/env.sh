# 被 fixture.sh、doctor.sh、drive.sh 引用：定位仓库和要驱动的 wrally。
# 驱动的是这份源码（uv run --project），不是全局安装的那一个；要换就设 WRALLY_BIN。
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
run_wrally() {
  if [ -n "${WRALLY_BIN:-}" ]; then "$WRALLY_BIN" "$@"; else uv run --quiet --project "$REPO" wrally "$@"; fi
}
