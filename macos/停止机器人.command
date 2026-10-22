#!/bin/bash
source "$(dirname "$0")/scripts/common.sh"
if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then exit 0; fi
"$PROJECT_DIR/.venv/bin/python" scripts/desktop.py stop
