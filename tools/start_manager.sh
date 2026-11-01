#!/bin/sh
# 不启动 ComfyUI 也能打开中转站管理器，优先用 ComfyUI 自己的 Python。用法：sh tools/start_manager.sh
cd "$(dirname "$0")/.." || exit 1
for py in ../../.venv/bin/python ../../venv/bin/python ../../../.venv/bin/python; do
    if [ -x "$py" ]; then
        exec "$py" manager_app.py "$@"
    fi
done
exec python3 manager_app.py "$@"
