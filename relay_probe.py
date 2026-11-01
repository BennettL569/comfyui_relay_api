"""探测中转站：连通性、站点程序、模型列表，并判断每个模型的用途（文本/图片/视频）和推荐的接口格式。"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter
from typing import Any
from urllib.parse import urlparse

try:
    from . import relay_client as rc
    from .relay_i18n import tr
except ImportError:  # 独立运行管理器时
    import relay_client as rc
    from relay_i18n import tr

CACHE_TTL = 600  # 秒
KIND_ORDER = {"image": 0, "video": 1, "text": 2, "other": 3}

MAX_MODELS = 5000
# 按模型名判断用途：先看明确写出模态的词，再看品牌名（sora_image、luma-photon 是生图模型）
OTHER_WORDS = ("embedding", "embed", "rerank", "tts", "whisper", "speech", "audio", "moderation", "transcribe", "realtime",
               "music", "suno", "lyrics")
VIDEO_TOKENS = ("video", "t2v", "i2v", "s2v", "2video")
IMAGE_TOKENS = ("image", "photon", "t2i", "i2i", "img")
VIDEO_WORDS = ("sora", "veo", "kling", "hailuo", "minimax-h", "vidu", "seedance", "runway", "luma", "pixverse", "pika",
               "mochi", "ltx", "jimeng-v", "jimeng_v", "wan2.", "wan3.", "wan 2", "wan 3")
IMAGE_WORDS = ("gpt-image", "dall-e", "dalle", "flux", "seedream", "seededit", "jimeng", "banana", "imagen",
               "midjourney", "mj_", "kontext", "ideogram", "recraft", "stable-diffusion", "sd3", "sdxl", "hidream",
               "cogview", "kolors", "dreamina", "wanx")
CHAT_IMAGE_WORDS = ("sora", "4o-image", "-exp-image")  # 这几类生图模型在中转站上通常走对话接口

# new-api 的任务插件认领的模型，/v1/models 里 owned_by 是这个值。
# 不按模型名猜插件键：站长可能绑了自定义插件（MiniMax-H3 不一定归官方的 hailuo），键不对时站点只回「无可用渠道」。
# 提交到 /v1/videos 时站点会按模型自己找插件，官方的视频插件都支持这个入口
TASK_OWNER = "task plugin"

_cache: dict[str, tuple[float, str, dict]] = {}  # 指纹 -> (时间, 中转站名, 结果)


def _has(model: str, words: tuple[str, ...]) -> bool:
    return any(w in model for w in words)


def _kind_from_name(m: str) -> str | None:
    if _has(m, OTHER_WORDS):
        return "other"
    if _has(m, VIDEO_TOKENS):
        return "video"
    if _has(m, IMAGE_TOKENS):
        return "image"
    if _has(m, VIDEO_WORDS):
        return "video"
    if _has(m, IMAGE_WORDS):
        return "image"
    return None


def classify(model_id: str, endpoint_types: list[str] | None = None, owned_by: str = "") -> dict[str, Any]:
    """返回 {kind, api_format, reason}。

    endpoint_types 是 new-api 在 /v1/models 里标注的接口类型（第一个是首选），one-api 等没有。
    注意 new-api 对没有专门标注的渠道（包括可灵等视频任务渠道）一律标 "openai"，
    所以 "openai" 只说明能走对话接口，不能据此判断图片/视频模型的格式。
    """
    m = model_id.lower()
    types = [t.lower() for t in endpoint_types or []]
    kind = _kind_from_name(m)

    if {"embeddings", "jina-rerank"} & set(types):
        return {"kind": "other", "api_format": None, "reason": ""}
    # 任务插件的模型不是对话模型：当成文本的话，「测一下」发一句话就可能生成一个要扣费的视频
    if owned_by.strip().lower() == TASK_OWNER and kind in (None, "video") and "image-generation" not in types:
        return {"kind": "video", "api_format": "task", "reason": tr("Marked by the relay as a task-plugin model; the relay finds the plugin from the model")}
    if "openai-video" in types:
        return {"kind": "video", "api_format": "openai", "reason": tr("Marked by the relay as using the OpenAI video API (/v1/videos)")}
    if "image-generation" in types:
        return {"kind": "image", **_image_format(m, types)}
    if kind == "other":
        return {"kind": "other", "api_format": None, "reason": ""}
    if kind == "video":
        return {"kind": "video", **_video_format(m)}
    if kind == "image":
        return {"kind": "image", **_image_format(m, types)}
    return {"kind": "text", "api_format": None, "reason": ""}


def _image_format(m: str, types: list[str]) -> dict[str, str]:
    if _has(m, CHAT_IMAGE_WORDS):
        return {"api_format": "chat", "reason": tr("On relays, this kind of image model usually goes through the chat API")}
    if "banana" in m or ("gemini" in m and "image" in m):
        if "gemini" in types:
            return {"api_format": "gemini", "reason": tr("Marked by the relay as supporting the native Gemini API, with aspect ratio and 4K options")}
        if types and types[0] == "image-generation":
            return {"api_format": "images", "reason": tr("Marked by the relay as using the image generation API (/v1/images)")}
        return {"api_format": "chat", "reason": tr("Most relays offer nano-banana through the chat API; if that fails, try the gemini format")}
    if _has(m, ("seedream", "seededit", "jimeng", "doubao")):
        return {"api_format": "images-json", "reason": tr("Seedream and Jimeng models take reference images in the image field")}
    return {"api_format": "images", "reason": tr("OpenAI image API (/v1/images)")}


def _video_format(m: str) -> dict[str, str]:
    if "sora" in m:
        return {"api_format": "openai", "reason": tr("Sora uses the OpenAI video API (/v1/videos); if that fails, try the chat API")}
    return {"api_format": "unified", "reason": tr("Unified video API (/v1/video/generations); if that fails, try the chat API")}


# ---------- 探测 ----------

def _fingerprint(relay: rc.Relay) -> str:
    key_hash = hashlib.sha256(relay.api_key.encode()).hexdigest()[:16]
    return f"{relay.base_url}|{relay.proxy or ''}|{key_hash}"


def _origin(relay: rc.Relay) -> str:
    parsed = urlparse(relay.root)
    return f"{parsed.scheme}://{parsed.netloc}"


def clear() -> None:
    """换语言后丢掉全部缓存：推荐理由和提示是按当时的语言生成的。"""
    _cache.clear()


def forget(name: str) -> None:
    """中转站被修改或删除后，丢掉它的缓存。"""
    for key in [k for k, (_, n, _) in _cache.items() if n == name]:
        _cache.pop(key, None)


async def _quiet(coro) -> Any:
    """站点信息、价格表只是锦上添花，拿不到就算了。"""
    try:
        return await coro
    except (rc.RelayError, asyncio.TimeoutError):
        return None


NEW_API_KEYS = ("HeaderNavModules", "setup", "quota_display_type", "self_use_mode_enabled", "enable_task")
ONE_API_KEYS = ("lark_client_id", "top_up_link", "chat_link")


async def _fetch_status(session, relay: rc.Relay) -> dict | None:
    """/api/status 是公开接口，不带令牌请求。system_name 是站长自己起的名字，程序类型看特有字段。"""
    data = await rc.request_json(session, relay, "GET", f"{_origin(relay)}/api/status", timeout=8, auth=False,
                                 max_bytes=2 << 20)
    info = data.get("data") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None
    if any(k in info for k in NEW_API_KEYS):
        kind = "New API"
    elif any(k in info for k in ONE_API_KEYS):
        kind = "One API"
    else:
        kind = ""
    site = str(info.get("system_name") or "").strip()[:60]
    version = str(info.get("version") or "").strip()[:40]
    if not (kind or site or version):
        return None
    return {"name": kind or site or tr("Unknown"), "site_name": site, "version": version}


async def _fetch_pricing(session, relay: rc.Relay) -> dict[str, list[str]]:
    """new-api 的价格表（站长可能关掉）。不能带令牌：带了会被当成管理员请求校验。"""
    data = await rc.request_json(session, relay, "GET", f"{_origin(relay)}/api/pricing", timeout=8, auth=False,
                                 max_bytes=10 << 20)
    items = data.get("data") if isinstance(data, dict) else None
    result = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("model_name") and isinstance(item.get("supported_endpoint_types"), list):
            result[str(item["model_name"])] = [str(t) for t in item["supported_endpoint_types"]]
    return result


QUERY_BLOCKED_MSG = ("This token is limited to specific models, and the relay does not let it check tasks on "
                     "/v1/video/generations or /v1/videos: it can submit and be charged, but never see the result. "
                     "Remove the token's model limit in the relay's console before generating video")
QUERY_BLOCKED_WORDS = ("无权访问模型", "no access to model")  # 站点自己的报错，语言看站点


async def _query_blocked(relay: rc.Relay) -> bool:
    """new-api 查任务前会先检查令牌的模型白名单，而查询请求里没有模型名，所以限定了模型的令牌一律 403。
    用不存在的任务 ID 问一下就知道，不创建任务，也不扣费。"""
    try:
        async with rc.new_session() as session:
            await rc.request_json(session, relay, "GET", "/v1/video/generations/relay-check", timeout=8,
                                  max_bytes=1 << 20)
    except rc.RelayError as e:
        return any(word in str(e).lower() for word in QUERY_BLOCKED_WORDS)
    except asyncio.TimeoutError:
        pass
    return False


async def _probe(relay: rc.Relay, timeout: float) -> dict:
    start = time.monotonic()
    async with rc.new_session() as session:
        status_task = asyncio.ensure_future(_quiet(_fetch_status(session, relay)))
        pricing_task = asyncio.ensure_future(_quiet(_fetch_pricing(session, relay)))
        try:
            data = await rc.request_json(session, relay, "GET", "/v1/models", timeout=timeout, max_bytes=20 << 20)
        except rc.RelayError as e:
            status_task.cancel()
            pricing_task.cancel()
            await asyncio.gather(status_task, pricing_task, return_exceptions=True)
            return {"ok": False, "error": str(e)}
        latency = round((time.monotonic() - start) * 1000)
        software, pricing = await status_task, await pricing_task

    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {"ok": False, "error": tr("Cannot understand the model list format: {detail}", detail=rc.short(data))}

    pricing = pricing or {}
    models, seen = [], set()
    for item in items[:MAX_MODELS]:
        if not isinstance(item, dict) or not item.get("id") or len(str(item["id"])) > 200 or str(item["id"]) in seen:
            continue
        model_id = str(item["id"])
        seen.add(model_id)
        types = item.get("supported_endpoint_types")
        types = [str(t)[:40] for t in types[:20]] if isinstance(types, list) else pricing.get(model_id, [])
        owned_by = str(item.get("owned_by") or "")
        models.append({"id": model_id, "owned_by": owned_by, "endpoint_types": types,
                       **classify(model_id, types, owned_by)})
    models.sort(key=lambda x: (KIND_ORDER[x["kind"]], x["id"].lower()))
    counts = Counter(m["kind"] for m in models)

    warnings = []
    if not models:
        warnings.append(tr("The relay returned an empty model list. The token may lack group permissions"))
    if not any(m["endpoint_types"] for m in models):
        warnings.append(tr("The relay does not mark which APIs each model supports, so recommended image and video formats are guessed from model names"))
    if any(m["api_format"] in ("unified", "openai") for m in models if m["kind"] == "video") and await _query_blocked(relay):
        warnings.append(tr(QUERY_BLOCKED_MSG))
    return {
        "ok": True,
        "error": "",
        "latency_ms": latency,
        "software": software,
        "models": models,
        "counts": {k: counts.get(k, 0) for k in ("text", "image", "video", "other")},
        "warnings": warnings,
        "checked_at": int(time.time()),
    }


async def probe(relay: rc.Relay, *, refresh: bool = False, timeout: float = 20.0) -> dict:
    """探测一个中转站，成功的结果缓存 10 分钟（按地址 + 令牌 + 代理区分）。"""
    key = _fingerprint(relay)
    cached = _cache.get(key)
    if not refresh and cached and time.time() - cached[0] < CACHE_TTL:
        return cached[2]
    result = await _probe(relay, timeout)
    if result["ok"]:
        _cache[key] = (time.time(), relay.name, result)
    return result
