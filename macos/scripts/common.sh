#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
umask 077
mkdir -p runtime/tmp runtime/cache/pycache logs
export TMPDIR="$PROJECT_DIR/runtime/tmp"
export PYTHONPYCACHEPREFIX="$PROJECT_DIR/runtime/cache/pycache"
export PYTHONUNBUFFERED=1
ensure_python() {
  if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
    /usr/bin/python3 "$PROJECT_DIR/scripts/bootstrap.py"
  fi
}
finish() {
  result=$?
  if [ "$result" -ne 0 ]; then printf '\n操作没有完成，请查看上方错误；可重新运行。\n'; fi
  if [ "$result" -ne 0 ] && [ -t 0 ]; then read -r -p "按回车关闭此窗口…" _reply; fi
  exit "$result"
}
trap finish EXIT
