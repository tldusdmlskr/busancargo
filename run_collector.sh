#!/bin/bash
# launchd가 주기적으로 호출하는 래퍼 스크립트
# collect_all.py 자체가 "지금이 목표 시각 창인지" 판단하므로 자주 호출해도 안전함

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/collector.log"

PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

{
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') 실행 시작 ====="
    "$PYTHON_BIN" collect_all.py
    echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') 실행 종료 ====="
} >> "$LOG_FILE" 2>&1
