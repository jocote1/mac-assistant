#!/usr/bin/env bash
# Launcher script for MacAssistant
cd "$(dirname "$0")"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

exec python3 main.py "$@"
