#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if ! /usr/bin/python3 scripts/build_macos_app.py; then
  printf '\n构建失败，请先安装 Xcode Command Line Tools 后重试。\n'
  read -r -p "按回车关闭…" _reply
  exit 1
fi
/usr/bin/open -R 栖.app
