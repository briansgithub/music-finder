#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.java_env" ]] && source "$ROOT/.java_env"
export PATH="${JAVA_HOME:+$JAVA_HOME/bin:}$PATH"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
exec "$PY" "$ROOT/cli.py" "$@"
