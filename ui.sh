#!/bin/zsh
cd "$(dirname "$0")"
# HF_HOME : seulement si non défini et que ~/Models existe (sinon cache HF par défaut)
[ -z "$HF_HOME" ] && [ -d "$HOME/Models" ] && export HF_HOME="$HOME/Models"
exec .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8723
