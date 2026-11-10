#!/usr/bin/env bash
# 只读检查：这份源码能不能被驱动。加 --remote 再查本机凭证能否调通远端（一次只读的 wrally projects）。
set -uo pipefail
. "$(dirname "$0")/env.sh"
bad=0
say() { printf '%-10s %s\n' "$1" "$2"; }
for tool in uv ffmpeg ffprobe; do
  if command -v "$tool" >/dev/null; then say ok "$tool"; else say MISSING "$tool"; bad=1; fi
done
where=$(cd "$REPO" && uv run --quiet python -c 'import wrally, pathlib; print(pathlib.Path(wrally.__file__).parent)' 2>&1)
if [ "$where" = "$REPO/src/wrally" ]; then
  say ok "wrally imports from $where"
else
  say WRONG "wrally imports from: $where (run: uv sync)"
  bad=1
fi
if run_wrally --help >/dev/null 2>&1; then say ok "wrally --help"; else say FAIL "wrally --help"; bad=1; fi
dirty=""
git -C "$REPO" diff --quiet HEAD -- src || dirty=" + uncommitted changes in src/"
say info "commit $(git -C "$REPO" rev-parse --short HEAD)$dirty"
if [ "${1:-}" = "--remote" ]; then
  if run_wrally projects >/dev/null 2>&1; then
    say ok "remote: wrally projects"
  else
    say FAIL "remote: wrally projects failed (run: wrally login)"
    bad=1
  fi
fi
exit $bad
