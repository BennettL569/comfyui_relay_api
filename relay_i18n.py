"""界面文字的翻译。源文字是英文，默认也显示英文。

ComfyUI 页面启动时会把用户的语言设置告诉后端（见 web/relay_manager.js），独立管理器用页面上的语言开关；
没有页面时（比如只调接口）可以设环境变量 COMFY_RELAY_LANG=zh。翻译都在 manager/zh.json，页面和后端共用一份。
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("comfyui_relay_api")

LANG_ENV = "COMFY_RELAY_LANG"
LANGUAGES = ("en", "zh")
CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manager", "zh.json")

_language = os.environ.get(LANG_ENV) if os.environ.get(LANG_ENV) in LANGUAGES else "en"
_zh: dict[str, str] | None = None


def language() -> str:
    return _language


def set_language(lang: str) -> None:
    global _language
    if lang not in LANGUAGES:
        raise ValueError(lang)
    _language = lang


def catalog() -> dict[str, str]:
    global _zh
    if _zh is None:
        try:
            with open(CATALOG, encoding="utf-8") as f:
                _zh = json.load(f)
        except (OSError, ValueError) as e:  # 翻译坏了只是显示英文，不能影响节点运行
            log.error("[relay] cannot read translations: %s", e)
            _zh = {}
    return _zh


def tr(text: str, **values) -> str:
    """翻译并填入变量。text 里的 {name} 是变量，变量的值不会再被解析。"""
    template = catalog().get(text, text) if _language == "zh" else text
    return template.format(**values) if values else template
