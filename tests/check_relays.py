"""检查 relays.json 里每个中转站能不能连通，并列出模型的分类和推荐接口。
只发免费的只读请求（模型列表、站点信息、用不存在的任务 ID 查询），不扣费，也不会打印 Key。

    python -X utf8 tests/check_relays.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay_client as rc  # noqa: E402
import relay_probe as rp  # noqa: E402

LABELS = {"image": "图片", "video": "视频"}


async def check(name: str) -> bool:
    try:
        result = await rp.probe(rc.get_relay(name), refresh=True, timeout=30)
    except rc.RelayError as e:
        print(f"✗ {name}：{e}")
        return False
    if not result["ok"]:
        print(f"✗ {name}：{result['error']}")
        return False
    counts = result["counts"]
    print(f"✓ {name}：连通，共 {len(result['models'])} 个模型"
          f"（文本 {counts['text']} · 图片 {counts['image']} · 视频 {counts['video']} · 其他 {counts['other']}）")
    for kind, label in LABELS.items():
        hits = [m for m in result["models"] if m["kind"] == kind]
        for m in hits[:12]:
            print(f"    {label}：{m['id']}（{m['api_format']}）")
        if len(hits) > 12:
            print(f"    {label}：还有 {len(hits) - 12} 个…")
    for warning in result["warnings"]:
        print(f"    ⚠ {warning}")
    return True


async def main() -> int:
    try:
        names = list(rc.load_relays())
    except rc.RelayError as e:
        print(e)
        return 1
    if not names:
        print(f"{rc.config_path()} 里还没有配置中转站")
        return 1
    results = [await check(name) for name in names]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
