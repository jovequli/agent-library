#!/bin/sh
# Start this checkout only; user libraries remain in AGENT_LIBRARY_HOME or --home.
set -eu
PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  AGENT_PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  AGENT_PYTHON="$(command -v python3)"
else
  printf '%s\n' 'Agent Library needs Python 3.11 or newer. Install Python, then open this file again.'
  exit 1
fi
"$AGENT_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Agent Library needs Python 3.11 or newer.")'
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$AGENT_PYTHON" -m agent_library serve --open "$@"
