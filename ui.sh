#!/bin/zsh
cd "$(dirname "$0")"
export HF_HOME=/Users/jules/Models
exec .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8723
