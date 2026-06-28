#!/usr/bin/env bash
# One command to run the test suite. Usage: ./run-tests.sh [optional path or -k filter]
# Watch for red (FAIL) vs green (passed). That's the whole signal.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/../.venv/bin/python" -m pytest "$HERE/tests" --no-cov "$@"
