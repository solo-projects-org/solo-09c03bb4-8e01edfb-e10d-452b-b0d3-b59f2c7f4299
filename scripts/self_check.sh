#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-python3}"

echo "== Syntax check =="
"$PYTHON_BIN" -m compileall -q wsgi.py meal_subsidy tests

echo "== Unit/integration tests =="
"$PYTHON_BIN" -m unittest discover -s tests -v
