#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE_DIR="$PROJECT_DIR/.chrome-profile"
DEBUG_PORT=9223
MODEL="${OLLAMA_MODEL:-qwen3:4b-instruct}"

echo "Setting up..."

# Virtual environment
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
fi

if ! "$PROJECT_DIR/.venv/bin/python" -c "import requests, playwright" 2>/dev/null; then
    echo "Installing dependencies..."
    "$PROJECT_DIR/.venv/bin/pip" install requests playwright
fi

echo "Python environment is ready."

# Ollama
if ! command -v ollama >/dev/null; then
    echo "Ollama is not installed."
    exit 1
fi

if ! curl -s http://127.0.0.1:11434 >/dev/null 2>&1; then
    echo "Starting Ollama..."
    ollama serve > "$PROJECT_DIR/ollama.log" 2>&1 &

    until curl -s http://127.0.0.1:11434 >/dev/null 2>&1; do
        sleep 1
    done
fi

if ! ollama list | grep -q "$MODEL"; then
    echo "Downloading $MODEL..."
    ollama pull "$MODEL"
fi

echo "Ollama is ready."

# Project directories
mkdir -p "$PROJECT_DIR/learned"
mkdir -p "$PROJECT_DIR/evidence"

# computer-agent alias
if [ "$(basename "$SHELL")" = "zsh" ]; then
    SHELL_RC="$HOME/.zshrc"
else
    SHELL_RC="$HOME/.bashrc"
fi

ALIAS="alias computer-agent='$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/computer-agent.py'"

touch "$SHELL_RC"
sed -i '' '/^alias computer-agent=/d' "$SHELL_RC"
echo "$ALIAS" >> "$SHELL_RC"

echo "computer-agent alias is ready."

# Chrome
if [ ! -d "/Applications/Google Chrome.app" ]; then
    echo "Google Chrome is not installed."
    exit 1
fi

mkdir -p "$PROFILE_DIR"

# Close only the automation Chrome
pkill -f "$PROFILE_DIR" 2>/dev/null || true
sleep 1

echo "Opening Chrome..."

open -na "Google Chrome" --args \
    --remote-debugging-port="$DEBUG_PORT" \
    --user-data-dir="$PROFILE_DIR"

echo
echo "Setup complete."
echo
echo "First run:"
echo "    source $SHELL_RC"
echo
echo "Next, open website and run:"
echo '    computer-agent "your goal"'