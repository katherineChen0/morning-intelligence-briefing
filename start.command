#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Open it, add your OPENAI_API_KEY and ACCESS_TOKEN, then run this file again."
  open -e .env
  exit 1
fi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
set -a
source .env
set +a
open "http://127.0.0.1:8000/health"
uvicorn app:app --host 0.0.0.0 --port 8000
