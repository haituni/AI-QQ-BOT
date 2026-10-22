#!/bin/bash
source "$(dirname "$0")/scripts/common.sh"
ensure_python
"$PROJECT_DIR/.venv/bin/python" scripts/control.py start
