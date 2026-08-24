#!/usr/bin/env bash
# Windows/WSL/Linux 공용 Python 다운로드 도구를 호출한다.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 tools/fetch_models.py
