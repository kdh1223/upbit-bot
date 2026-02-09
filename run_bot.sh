#!/usr/bin/env bash
set -euo pipefail

if screen -list | grep -q "\.bot"; then
  echo "screen session 'bot' already exists. stop it first."
  exit 1
fi

if [ -f ".venv/bin/activate" ]; then
  . ".venv/bin/activate"
elif [ -f "venv/bin/activate" ]; then
  . "venv/bin/activate"
fi

cd ~/upbit-bot && screen -S bot -L -Logfile bot_console.log python main.py
