#!/bin/sh
# Opens the relay manager without starting ComfyUI, preferring ComfyUI's own Python. Usage: sh tools/start_manager.sh
cd "$(dirname "$0")/.." || exit 1
for py in ../../.venv/bin/python ../../venv/bin/python ../../../.venv/bin/python; do
    if [ -x "$py" ]; then
        exec "$py" manager_app.py "$@"
    fi
done
exec python3 manager_app.py "$@"
