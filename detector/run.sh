#!/bin/zsh

# Change to the script directory
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# Clear screen and show header
clear
echo "═══════════════════════════════════════════════════════"
echo "  Frigate Detector - Apple Silicon Edition"
echo "═══════════════════════════════════════════════════════"
echo ""

# Choose Python per policy:
# 1) Use `python3` if it is >= 3.11
# 2) Else try `python3.11`
# 3) Else error

use_py3() {
  python3 - <<'PYVER' 2>/dev/null
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYVER
}

if command -v python3 >/dev/null 2>&1 && use_py3; then
  PYBIN="python3"
elif command -v python3.11 >/dev/null 2>&1; then
  PYBIN="python3.11"
else
  echo "❌ ERROR: Python 3.11 is required."
  echo "   Please install it (e.g., 'brew install python@3.11') and try again."
  echo ""
  echo "Press any key to close this window..."
  read -n 1
  exit 1
fi

echo "✓ Using Python: $PYBIN ($($PYBIN --version 2>&1))"
echo ""

# Setup virtual environment
if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  if ! "$PYBIN" -m venv venv; then
    echo "❌ ERROR: Failed to create virtual environment"
    echo ""
    echo "Press any key to close this window..."
    read -n 1
    exit 1
  fi
  echo "✓ Virtual environment created"
else
  echo "✓ Virtual environment found"
fi
echo ""

PIP="venv/bin/pip3"
PY="venv/bin/python3"

# Install dependencies
echo "Installing dependencies..."
if ! "$PIP" install --quiet --upgrade pip || ! "$PIP" install --quiet -r requirements.txt; then
  echo "❌ ERROR: Failed to install dependencies"
  echo "   Check the log file for details: $HOME/Library/Logs/FrigateDetector/FrigateDetector.log"
  echo ""
  echo "Press any key to close this window..."
  read -n 1
  exit 1
fi
echo "✓ Dependencies installed"
echo ""

# Setup logging
LOG_DIR="$HOME/Library/Logs/FrigateDetector"
LOG_FILE="$LOG_DIR/FrigateDetector.log"
mkdir -p "$LOG_DIR"

echo "═══════════════════════════════════════════════════════"
echo "  Starting detector..."
echo "═══════════════════════════════════════════════════════"
echo "  Log file: $LOG_FILE"
echo "  Model: AUTO"
echo ""
echo "  (This window will stay open while the detector is running)"
echo "  (Press Ctrl+C to stop the detector)"
echo "═══════════════════════════════════════════════════════"
echo ""

# Run the detector with both console and log output
"$PY" detector/zmq_onnx_client.py --model AUTO 2>&1 | tee -a "$LOG_FILE"

# If we get here, the detector has stopped
EXIT_CODE=$?
echo ""
echo "═══════════════════════════════════════════════════════"
if [ $EXIT_CODE -eq 0 ]; then
  echo "  Detector stopped normally"
else
  echo "  Detector stopped with error (exit code: $EXIT_CODE)"
  echo "  Check the log file for details: $LOG_FILE"
fi
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Press any key to close this window..."
read -n 1
