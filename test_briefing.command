#!/bin/bash
cd "$(dirname "$0")"
set -a
source .env
set +a
open "http://127.0.0.1:8000/audio/today?token=${ACCESS_TOKEN}"
