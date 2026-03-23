#!/usr/bin/env bash
# Install TrainingEdge in a virtual environment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"

echo "=== TrainingEdge Installer ==="
echo "Project: $PROJECT_DIR"

# Create venv if needed
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    # Prefer newer Python
    PYTHON=""
    for p in python3.13 python3.12 python3.11 python3; do
        if command -v "$p" &>/dev/null; then PYTHON="$p"; break; fi
    done
    echo "Using: $PYTHON ($($PYTHON --version))"
    "$PYTHON" -m venv "$VENV_DIR"
fi

echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$PROJECT_DIR"

echo ""
echo "Done. To run:"
echo "  $VENV_DIR/bin/python -m scripts.cli sync --days 7"
echo "  $VENV_DIR/bin/uvicorn api.app:app --host 0.0.0.0 --port 8420"
