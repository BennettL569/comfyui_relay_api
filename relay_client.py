"""中转站 API 客户端：读取 relays.json、发请求、解析各家不同的返回格式。

只依赖 aiohttp，不依赖 ComfyUI，方便单独测试。图片和视频一律以 bytes 进出。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import aiohttp

log = logging.getLogger("comfyui_relay_api")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_ENV = "COMFY_RELAY_CONFIG"

SUCCESS_STATUSES = {"completed", "complete", "succeeded", "succeed", "success", "successful", "done", "finished"}
FAILURE_STATUSES = {"failed", "fail", "failure", "error", "cancelled", "canceled", "expired", "rejected", "timeout"}
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".m4v")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")

DATA_URL_RE = re.compile(r"data:(image|video)/[\w.+-]+;base64,[A-Za-z0-9+/=]+")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
MD_DATA_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*<?data:image/[^)]*\)")
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}　-〿＀-￯]+")
URL_TAIL = ".,;:!?"

CheckInterrupt = Callable[[], None] | None
OnProgress = Callable[[float], None] | None
OnStatus = Callable[[str], None] | None

_SSE_DONE = object()


class RelayError(Exception):
    """直接显示给用户看的错误。"""


# ---------- 配置 ----------

def config_path() -> str:
    return os.environ.get(CONFIG_ENV) or os.path.join(PLUGIN_DIR, "relays.json")


def _read_config_text(path: str) -> str:
    """读配置文件。另一个进程正好在替换文件时 Windows 上会短暂打不开，重试几次；
    手动编辑后存成 GBK 或 UTF-16 的也尽量认出来。"""
    for attempt in range(20):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            break
        except PermissionError:
            if attempt == 19:
                raise RelayError(f"{path} 正被其他程序占用，请稍后再试") from None
            time.sleep(0.05)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RelayError(f"{path} 的编码认不出来，请用记事本「另存为」并把编码选成 UTF-8")


def read_config() -> dict[str, Any]:
    """读取完整的 relays.json（包括以 _ 开头的说明字段），保持原有顺序。"""
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(_read_config_text(path))
    except json.JSONDecodeError as e:
        raise RelayError(f"{path} 不是合法的 JSON：{e}（上一次保存的版本在 relays.json.bak）") from None
    if not isinstance(data, dict):
        raise RelayError(f"{path} 最外层必须是 JSON 对象")
    return data


def load_relays() -> dict[str, dict]:
    return {name: cfg for name, cfg in read_config().items() if not name.startswith("_") and isinstance(cfg, dict)}


def _unfilled(value: str) -> bool:
    return not value or "你的" in value or "填" in value


class Relay:
    def __init__(self, name: str, base_url: str, api_key: str, proxy: str | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.proxy = proxy
        # 去掉结尾的 /v1，各接口路径自己带版本号（/v1/...、/v1beta/...）
        self.root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    def url(self, path: str) -> str:
        """拼出请求地址。完整网址只允许中转站自己的，否则令牌会被发到别的网站
        （例如别人分享的工作流在自定义路径里填了一个外部网址）。"""
        if path.startswith(("http://", "https://")):
            if not self.same_origin(path):
                raise RelayError(f"只能请求中转站自己的地址，不能请求 {urlparse(path).netloc}（防止令牌被发到别的网站）")
            return path
        return f"{self.root}/{path.lstrip('/')}"

    def resolve(self, url: str) -> str:
        """补全接口返回的相对地址。"""
        if url.startswith(("http://", "https://", "data:")):
            return url
        return urljoin(self.root + "/", url)

    def headers(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", **(extra or {})}

    def same_origin(self, url: str) -> bool:
        a, b = urlparse(url), urlparse(self.root)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def get_relay(name: str) -> Relay:
    cfg = load_relays().get(name)
    if cfg is None:
        raise RelayError(f"relays.json 里没有名为「{name}」的中转站（{config_path()}）")
    base_url = str(cfg.get("base_url") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key and cfg.get("api_key_env"):
        api_key = os.environ.get(str(cfg["api_key_env"]), "").strip()
    if _unfilled(base_url) or not base_url.startswith(("http://", "https://")):
        raise RelayError(f"中转站「{name}」的 base_url 还没填好，应类似 https://api.example.com/v1")
    if _unfilled(api_key):
        raise RelayError(f"中转站「{name}」的 api_key 还没填（{config_path()}）")
    return Relay(name, base_url, api_key, str(cfg.get("proxy") or "").strip() or None)


# ---------- 请求体与文本工具 ----------

def parse_extra(text: str | None) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RelayError(f"额外参数不是合法的 JSON：{e}") from None
    if not isinstance(data, dict):
        raise RelayError('额外参数必须是 JSON 对象，例如 {"quality": "high"}')
    return data


def merge_extra(body: dict, extra: dict) -> dict:
    """把 extra 深度合并进 body，值为 null 的键会被删掉。"""
    for key, value in extra.items():
        if value is None:
            body.pop(key, None)
        elif isinstance(value, dict) and isinstance(body.get(key), dict):
            merge_extra(body[key], value)
        else:
            body[key] = value
    return body


def short(value: Any, limit: int = 600) -> str:
    """截断并省略 base64，用于错误信息和日志。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = DATA_URL_RE.sub("data:…(base64)", text)
    text = re.sub(r"[A-Za-z0-9+/=]{300,}", "…(base64)", text)
    return text if len(text) <= limit else text[:limit] + "…"


def to_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def decode_data_url(url: str) -> bytes:
    header, _, payload = url.partition(",")
    if ";base64" not in header:
        raise RelayError("不支持非 base64 的 data URL")
    return base64.b64decode(payload)


def mime_ext(mime: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".bin")


def form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def chat_content(prompt: str, images: list[tuple[bytes, str]]) -> str | list[dict]:
    """OpenAI 格式的 user content；有图时用多段格式，图片在前。"""
    if not images:
        return prompt
    parts: list[dict] = [{"type": "image_url", "image_url": {"url": to_data_url(b, m)}} for b, m in images]
    parts.append({"type": "text", "text": prompt})
    return parts


def strip_data_urls(text: str) -> str:
    text = MD_DATA_IMAGE_RE.sub("[图片]", text)
    return DATA_URL_RE.sub("[图片]", text).strip()


def _urls(text: str) -> list[str]:
    return [u.rstrip(URL_TAIL) for u in URL_RE.findall(text)]


# ---------- HTTP ----------

def _timeout(seconds: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=seconds, sock_connect=30)


def new_session() -> aiohttp.ClientSession:
    # trust_env=True：没在 relays.json 里配 proxy 时，认系统的 HTTP(S)_PROXY 环境变量
    return aiohttp.ClientSession(trust_env=True)


MAX_RESPONSE = 300 * 1024 * 1024  # 生图接口直接返回 base64 图片，4K 图可能几十 MB
MAX_DOWNLOAD = 2 * 1024 * 1024 * 1024


async def _read_limited(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    """读响应体但设上限，防止恶意站点返回超大内容把内存撑爆。"""
    if resp.content_length is not None and resp.content_length > limit:
        raise RelayError(f"返回内容太大（{resp.content_length / 1048576:.0f} MB），已放弃")
    chunks, size = [], 0
    async for chunk in resp.content.iter_chunked(1 << 16):
        size += len(chunk)
        if size > limit:
            raise RelayError(f"返回内容太大（超过 {limit / 1048576:.0f} MB），已放弃")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def describe_body(text: str, content_type: str = "") -> str:
    """网页（Cloudflare 拦截页、站点首页）只报标题，不把一大段 HTML 甩给用户。"""
    if content_type == "text/html" or text.lstrip()[:1] == "<":
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        title = " ".join(m.group(1).split())[:80] if m else ""
        return (f"站点返回的是网页而不是接口数据{f'（页面标题：{title}）' if title else ''}。"
                "可能是接口地址填成了网站首页，或者被 Cloudflare 拦截了（可以试试配置代理）")
    try:
        data = json.loads(text)
    except ValueError:
        return short(text)
    # JSON 错误只留站点的原话：有的站把中文转义成 \uXXXX，原样显示看不懂
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str) and error["message"]:
        code = error.get("code")
        return short(f"{error['message']}（{code}）" if code and isinstance(code, str) else error["message"])
    if isinstance(error, str) and error:
        return short(error)
    return short(data)


async def request_json(
    session: aiohttp.ClientSession,
    relay: Relay,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    form: aiohttp.FormData | None = None,
    headers: dict | None = None,
    timeout: float = 120.0,
    auth: bool = True,
    max_bytes: int = MAX_RESPONSE,
) -> Any:
    url = relay.url(path)
    try:
        async with session.request(
            method, url, json=json_body, data=form, headers=relay.headers(headers) if auth else headers,
            proxy=relay.proxy, timeout=_timeout(timeout),
        ) as resp:
            status, ctype = resp.status, resp.content_type
            text = _decode(await _read_limited(resp, max_bytes), resp.charset)
    except TimeoutError:
        raise RelayError(f"请求超时（{timeout:g} 秒）：{method} {url}") from None
    except aiohttp.ClientError as e:
        raise RelayError(f"连不上中转站「{relay.name}」：{e}") from None
    if status >= 400:
        raise RelayError(f"{method} {url} 返回 HTTP {status}：{describe_body(text, ctype)}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RelayError(f"{method} {url} 返回的不是 JSON：{describe_body(text, ctype)}") from None


def raise_for_error_payload(data: Any, what: str) -> None:
    """有的站出错也回 HTTP 200，把错误放在 body 的 error 字段里。"""
    if not isinstance(data, dict) or not data.get("error"):
        return
    err = data["error"]
    message = (err.get("message") or err.get("msg")) if isinstance(err, dict) else err
    raise RelayError(f"{what}失败：{short(message or err)}")


async def guarded(coro: Awaitable, check_interrupt: CheckInterrupt) -> Any:
    """等待请求的同时每 0.5 秒检查一次用户是否点了取消。"""
    if check_interrupt is None:
        return await coro
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                return task.result()
            check_interrupt()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def sleep_checked(seconds: float, check_interrupt: CheckInterrupt) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while (left := end - loop.time()) > 0:
        if check_interrupt:
            check_interrupt()
        await asyncio.sleep(min(0.5, left))


async def download(session: aiohttp.ClientSession, relay: Relay, url: str, *, timeout: float = 600.0) -> bytes:
    if url.startswith("data:"):
        return decode_data_url(url)
    url = relay.resolve(url)
    # 只有和中转站同源的地址才带 Key，避免把 Key 发给第三方存储
    headers = relay.headers() if relay.same_origin(url) else None
    try:
        async with session.get(url, headers=headers, proxy=relay.proxy, timeout=_timeout(timeout)) as resp:
            status, ctype = resp.status, resp.content_type
            body = await _read_limited(resp, MAX_DOWNLOAD)
    except TimeoutError:
        raise RelayError(f"下载超时：{url}") from None
    except aiohttp.ClientError as e:
        raise RelayError(f"下载失败：{url}：{e}") from None
    if status >= 400:
        raise RelayError(f"下载 {url} 返回 HTTP {status}：{describe_body(_decode(body[:200_000], None), ctype)}")
    if (ctype == "application/json" or ctype.startswith("text/")) and len(body) < 100_000:
        raise RelayError(f"下载 {url} 拿到的不是文件：{describe_body(_decode(body, None), ctype)}")
    return body


# ---------- 对话接口 ----------

def _first_message(data: Any) -> dict:
    try:
        message = data["choices"][0]["message"] or {}
    except (KeyError, IndexError, TypeError):
        raise RelayError(f"返回里没有 choices[0].message：{short(data)}") from None
    if not isinstance(message, dict):
        raise RelayError(f"返回的消息格式不对：{short(data)}")
    return message


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text") or "" for p in content if isinstance(p, dict))
    return ""


def _parse_sse_line(line: bytes) -> Any:
    line = line.strip()
    if not line.startswith(b"data:"):
        return None
    payload = line[5:].strip()
    if payload == b"[DONE]":
        return _SSE_DONE
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


async def _iter_sse(resp: aiohttp.ClientResponse) -> AsyncIterator[dict]:
    buffer, total = b"", 0
    async for chunk in resp.content.iter_any():
        total += len(chunk)
        if total > MAX_RESPONSE:
            raise RelayError("返回内容太大，已放弃")
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        for line in lines:
            event = _parse_sse_line(line)
            if event is _SSE_DONE:
                return
            if event is not None:
                yield event
    event = _parse_sse_line(buffer)
    if isinstance(event, dict):
        yield event


async def _chat_stream(session: aiohttp.ClientSession, relay: Relay, body: dict, timeout: float) -> tuple[str, dict]:
    url = relay.url("/v1/chat/completions")
    content, reasoning, images = [], [], []
    try:
        async with session.post(url, json=body, headers=relay.headers(), proxy=relay.proxy, timeout=_timeout(timeout)) as resp:
            if resp.status >= 400:
                text = _decode(await _read_limited(resp, 1 << 20), resp.charset)
                raise RelayError(f"POST {url} 返回 HTTP {resp.status}：{describe_body(text, resp.content_type)}")
            if resp.content_type != "text/event-stream":
                # 有的站不管 stream 参数，直接一次性返回
                text = _decode(await _read_limited(resp, MAX_RESPONSE), resp.charset)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    raise RelayError(f"POST {url} 返回的不是 JSON：{describe_body(text, resp.content_type)}") from None
                raise_for_error_payload(data, "对话请求")
                message = _first_message(data)
                return message_text(message), message
            async for event in _iter_sse(resp):
                raise_for_error_payload(event, "对话请求")
                for choice in event.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or choice.get("message") or {}
                    if not isinstance(delta, dict):
                        continue
                    if isinstance(delta.get("content"), str):
                        content.append(delta["content"])
                    if isinstance(delta.get("reasoning_content"), str):
                        reasoning.append(delta["reasoning_content"])
                    if isinstance(delta.get("images"), list):
                        images.extend(delta["images"])
    except TimeoutError:
        raise RelayError(f"请求超时（{timeout:g} 秒）：POST {url}") from None
    except aiohttp.ClientError as e:
        raise RelayError(f"连不上中转站「{relay.name}」：{e}") from None
    message = {"content": "".join(content), "reasoning_content": "".join(reasoning), "images": images}
    return message["content"], message


async def chat_completion(
    relay: Relay, body: dict, *, timeout: float, check_interrupt: CheckInterrupt, stream: bool = False
) -> tuple[str, dict]:
    """调用 /v1/chat/completions，返回 (回复文本, 消息对象)。"""
    async with new_session() as session:
        if stream:
            return await guarded(_chat_stream(session, relay, {**body, "stream": True}, timeout), check_interrupt)
        data = await guarded(
            request_json(session, relay, "POST", "/v1/chat/completions", json_body=body, timeout=timeout),
            check_interrupt,
        )
    raise_for_error_payload(data, "对话请求")
    message = _first_message(data)
    return message_text(message), message


async def list_models(relay: Relay, *, timeout: float = 60.0) -> list[str]:
    async with new_session() as session:
        data = await request_json(session, relay, "GET", "/v1/models", timeout=timeout)
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RelayError(f"看不懂模型列表的格式：{short(data)}")
    return sorted({str(i["id"]) for i in items if isinstance(i, dict) and i.get("id")})


# ---------- 生图 ----------

def find_image_refs(message: dict) -> list[str]:
    """从对话回复里找图片：images 字段、多段 content、文字里的 data URL / Markdown 图片 / 图片链接。"""
    refs: list[str] = []
    for item in message.get("images") or []:
        if isinstance(item, dict):
            image_url = item.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url or item.get("url")
            if url:
                refs.append(url)
    texts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if url:
                refs.append(url)
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
    for text in texts:
        refs += [m.group(0) for m in DATA_URL_RE.finditer(text) if m.group(1) == "image"]
        refs += [u for u in MD_IMAGE_RE.findall(text) if not u.startswith("data:")]
        refs += [u for u in _urls(text) if urlparse(u).path.lower().endswith(IMAGE_EXTS)]
    return list(dict.fromkeys(refs))


async def images_api(
    relay: Relay, model: str, prompt: str, refs: list[tuple[bytes, str]], *,
    size: str, n: int, quality: str, ref_mode: str, extra: dict, timeout: float, check_interrupt: CheckInterrupt,
) -> tuple[list[bytes], str]:
    """OpenAI 图像接口。ref_mode="edits" 时参考图多部分上传到 /v1/images/edits，
    ref_mode="json" 时参考图作为 image 字段发到 /v1/images/generations（Seedream 等）。"""
    fields: dict[str, Any] = {"model": model, "prompt": prompt}
    if size:
        fields["size"] = size
    if n > 1:
        fields["n"] = n
    if quality:
        fields["quality"] = quality
    async with new_session() as session:
        if refs and ref_mode == "edits":
            form = aiohttp.FormData(default_to_multipart=True)
            for key, value in merge_extra(fields, extra).items():
                form.add_field(key, form_value(value))
            name = "image[]" if len(refs) > 1 else "image"
            for i, (blob, mime) in enumerate(refs):
                form.add_field(name, blob, filename=f"image{i}{mime_ext(mime)}", content_type=mime)
            data = await guarded(
                request_json(session, relay, "POST", "/v1/images/edits", form=form, timeout=timeout), check_interrupt
            )
        else:
            if refs:
                urls = [to_data_url(b, m) for b, m in refs]
                fields["image"] = urls[0] if len(urls) == 1 else urls
            data = await guarded(
                request_json(session, relay, "POST", "/v1/images/generations",
                             json_body=merge_extra(fields, extra), timeout=timeout),
                check_interrupt,
            )
        raise_for_error_payload(data, "生图请求")
        items = data.get("data") if isinstance(data, dict) else None
        images, notes = [], []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("b64_json"):
                b64 = item["b64_json"]
                images.append(decode_data_url(b64) if b64.startswith("data:") else base64.b64decode(b64))
            elif item.get("url"):
                images.append(await guarded(download(session, relay, item["url"]), check_interrupt))
            if item.get("revised_prompt"):
                notes.append(item["revised_prompt"])
    if not images:
        raise RelayError(f"返回里没有图片：{short(data)}")
    return images, "\n".join(notes)


async def chat_image(
    relay: Relay, model: str, prompt: str, refs: list[tuple[bytes, str]], *,
    extra: dict, timeout: float, check_interrupt: CheckInterrupt,
) -> tuple[list[bytes], str]:
    """走对话接口出图（多数中转站的 nano-banana 等）。"""
    body = merge_extra({"model": model, "messages": [{"role": "user", "content": chat_content(prompt, refs)}]}, extra)
    text, message = await chat_completion(
        relay, body, timeout=timeout, check_interrupt=check_interrupt, stream=bool(body.get("stream"))
    )
    found = find_image_refs(message)
    if not found:
        raise RelayError(f"模型没有返回图片，回复内容：{short(text) or '（空）'}")
    async with new_session() as session:
        images = [await guarded(download(session, relay, ref), check_interrupt) for ref in found]
    return images, strip_data_urls(text)


async def gemini_image(
    relay: Relay, model: str, prompt: str, refs: list[tuple[bytes, str]], *,
    aspect_ratio: str, image_size: str, extra: dict, timeout: float, check_interrupt: CheckInterrupt,
) -> tuple[list[bytes], str]:
    """Gemini 原生 generateContent 接口。"""
    parts: list[dict] = [{"inline_data": {"mime_type": m, "data": base64.b64encode(b).decode()}} for b, m in refs]
    parts.append({"text": prompt})
    image_config = {}
    if aspect_ratio and aspect_ratio != "auto":
        image_config["aspectRatio"] = aspect_ratio
    if image_size and image_size != "auto":
        image_config["imageSize"] = image_size
    config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
    if image_config:
        config["imageConfig"] = image_config
    body = merge_extra({"contents": [{"role": "user", "parts": parts}], "generationConfig": config}, extra)
    path = f"/v1beta/models/{model.removeprefix('models/')}:generateContent"
    async with new_session() as session:
        data = await guarded(
            request_json(session, relay, "POST", path, json_body=body,
                         headers={"x-goog-api-key": relay.api_key}, timeout=timeout),
            check_interrupt,
        )
    raise_for_error_payload(data, "Gemini 请求")
    candidates = data.get("candidates") if isinstance(data, dict) else None
    images, texts = [], []
    for cand in candidates or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("thought"):  # 思考过程里的草稿，不要
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                images.append(base64.b64decode(inline["data"]))
            elif isinstance(part.get("text"), str):
                texts.append(part["text"])
    if not images:
        feedback = data.get("promptFeedback") if isinstance(data, dict) else None
        reason = (feedback or {}).get("blockReason") or ", ".join(
            str(c["finishReason"]) for c in candidates or [] if c.get("finishReason")
        )
        raise RelayError(f"Gemini 没有返回图片（{reason or '原因未知'}），回复内容：{short(''.join(texts)) or '（空）'}")
    return images, "\n".join(texts).strip()


# ---------- 视频 ----------

_URL_KEYS = ("url", "uri", "video_url", "videourl", "download_url", "file_url", "result_url", "output", "content_url")
_SKIP_KEY_PARTS = ("thumb", "cover", "poster", "image", "frame", "avatar", "icon", "preview")


def _walk(data: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _walk(v, str(k))
    elif isinstance(data, list):
        for item in data:
            yield from _walk(item, key)
    else:
        yield key, data


def _containers(data: Any) -> list[dict]:
    """顶层和常见的包装层（data / result / task / output）。"""
    if not isinstance(data, dict):
        return []
    found = [data]
    for k in ("data", "result", "task", "output", "response"):
        if isinstance(data.get(k), dict):
            found.append(data[k])
    return found


def _first_value(data: Any, keys: tuple[str, ...]) -> Any:
    containers = _containers(data)
    for k in keys:
        for c in containers:
            v = c.get(k)
            if v not in (None, "", [], {}):
                return v
    return None


def find_status(data: Any) -> str | None:
    v = _first_value(data, ("status", "task_status", "state", "taskStatus"))
    return v.strip().lower() if isinstance(v, str) else None


def find_task_id(data: Any) -> str | None:
    v = _first_value(data, ("task_id", "taskId", "id", "generation_id"))
    return str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else None


def find_progress(data: Any) -> float | None:
    v = _first_value(data, ("progress", "percent", "percentage"))
    if isinstance(v, str):
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", v)
        v = float(m.group(1)) if m else None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return max(0.0, min(100.0, float(v)))
    return None


def find_error_message(data: Any) -> str | None:
    v = _first_value(data, ("error", "fail_reason", "failure_reason", "error_message", "err_msg",
                            "task_status_msg", "message", "msg"))
    if isinstance(v, dict):
        v = v.get("message") or v.get("msg") or json.dumps(v, ensure_ascii=False)
    return str(v) if v else None


def find_video_url(data: Any) -> str | None:
    """在任意结构的 JSON 里找最像视频地址的字段。"""
    best, best_score = None, 0
    for key, value in _walk(data):
        if not isinstance(value, str):
            continue
        v, k = value.strip(), key.lower()
        if v.startswith("data:video/"):
            return v
        if not v.startswith(("http://", "https://", "/")) or any(p in k for p in _SKIP_KEY_PARTS):
            continue
        path = urlparse(v).path.lower()
        if path.endswith(IMAGE_EXTS):
            continue
        score = (3 if path.endswith(VIDEO_EXTS) else 0) + (2 if "video" in k else 0) + (1 if k in _URL_KEYS else 0)
        if score > best_score:
            best, best_score = v, score
    return best


def find_video_url_in_text(text: str) -> str | None:
    for m in DATA_URL_RE.finditer(text):
        if m.group(1) == "video":
            return m.group(0)
    urls = _urls(text)
    for u in urls:
        if urlparse(u).path.lower().endswith(VIDEO_EXTS):
            return u
    for u in urls:
        if "video" in u.lower() and not urlparse(u).path.lower().endswith(IMAGE_EXTS):
            return u
    return None


MAX_POLL_FAILURES = 5  # 任务已经提交，可能已经扣费，查询偶尔失败不能直接放弃


async def poll_task(
    fetch: Callable[[], Awaitable[Any]], *, label: str, poll_interval: float, timeout: float,
    check_interrupt: CheckInterrupt, on_progress: OnProgress, on_status: OnStatus = None, task_id: str = "",
) -> Any:
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + timeout
    last_status: Any = object()
    failures = 0
    tag = f"（任务 ID：{task_id}）" if task_id else ""
    while True:
        try:
            data = await guarded(fetch(), check_interrupt)
        except RelayError as e:
            failures += 1
            if failures >= MAX_POLL_FAILURES or loop.time() >= deadline:
                raise RelayError(f"{label}查询失败{tag}，任务可能还在运行，可以到站点后台查看：{e}") from None
            log.warning("[中转站] %s 查询失败（第 %d 次）：%s", label, failures, e)
            if on_status:
                on_status(f"{label}：查询失败，第 {failures} 次，稍后重试…")
            await sleep_checked(poll_interval, check_interrupt)
            continue
        failures = 0
        status = find_status(data)
        if status != last_status:
            log.info("[中转站] %s 状态：%s  %s", label, status, short(data, 300))
            last_status = status
        progress = find_progress(data)
        if on_progress and progress is not None:
            on_progress(progress)
        if on_status:
            percent = f" {progress:.0f}%" if progress is not None else ""
            on_status(f"{label}：{status or '处理中'}{percent}，已等待 {loop.time() - start:.0f} 秒")
        if status in SUCCESS_STATUSES or (status is None and find_video_url(data)):
            return data
        if status in FAILURE_STATUSES:
            raise RelayError(f"{label}失败{tag}：{find_error_message(data) or short(data)}")
        if loop.time() >= deadline:
            raise RelayError(f"{label}等待超时（{timeout:g} 秒）{tag}，最后状态：{status or '未知'}")
        await sleep_checked(poll_interval, check_interrupt)


async def _download_video(
    session: aiohttp.ClientSession, relay: Relay, url: str, check_interrupt: CheckInterrupt
) -> tuple[bytes, str]:
    video = await guarded(download(session, relay, url), check_interrupt)
    return video, "" if url.startswith("data:") else relay.resolve(url)


async def _finish_video_task(
    session: aiohttp.ClientSession, relay: Relay, created: Any, query_path: str, **wait: Any
) -> tuple[bytes, str]:
    """提交后的通用流程：直接给了地址就下载，否则按任务 ID 轮询。"""
    status = find_status(created)
    if status in FAILURE_STATUSES:
        raise RelayError(f"视频任务提交失败：{find_error_message(created) or short(created)}")
    task_id = find_task_id(created)
    url = find_video_url(created) if (status in SUCCESS_STATUSES or not task_id or not query_path) else None
    if not url:
        if not task_id or not query_path:
            raise RelayError(f"提交后没拿到任务 ID 或视频地址，返回内容：{short(created)}")
        log.info("[中转站] 视频任务已提交，任务 ID：%s", task_id)
        path = query_path.replace("{task_id}", task_id).replace("{id}", task_id)
        result = await poll_task(
            lambda: request_json(session, relay, "GET", path, timeout=60), label="视频任务", task_id=task_id, **wait
        )
        url = find_video_url(result)
        if not url:
            raise RelayError(f"任务已完成，但返回里找不到视频地址（任务 ID：{task_id}）：{short(result)}")
    return await _download_video(session, relay, url, wait["check_interrupt"])


async def video_unified(relay: Relay, body: dict, **wait: Any) -> tuple[bytes, str]:
    """new-api 等的统一视频格式：POST /v1/video/generations，GET /v1/video/generations/{task_id}。"""
    async with new_session() as session:
        created = await guarded(
            request_json(session, relay, "POST", "/v1/video/generations", json_body=body), wait["check_interrupt"]
        )
        return await _finish_video_task(session, relay, created, "/v1/video/generations/{task_id}", **wait)


async def video_openai(
    relay: Relay, fields: dict, reference: tuple[bytes, str] | None, *, multipart: bool, **wait: Any
) -> tuple[bytes, str]:
    """OpenAI / Sora 格式：POST /v1/videos，轮询 GET /v1/videos/{id}，从 /content 下载。"""
    check_interrupt = wait["check_interrupt"]
    async with new_session() as session:
        if multipart:
            form = aiohttp.FormData(default_to_multipart=True)
            for key, value in fields.items():
                form.add_field(key, form_value(value))
            if reference:
                blob, mime = reference
                form.add_field("input_reference", blob, filename="reference" + mime_ext(mime), content_type=mime)
            created = await guarded(request_json(session, relay, "POST", "/v1/videos", form=form), check_interrupt)
        else:
            body = dict(fields)
            if reference:
                body["input_reference"] = to_data_url(*reference)
            created = await guarded(request_json(session, relay, "POST", "/v1/videos", json_body=body), check_interrupt)
        video_id = find_task_id(created)
        if find_status(created) in FAILURE_STATUSES or not video_id:
            raise RelayError(f"视频任务提交失败：{find_error_message(created) or short(created)}")
        log.info("[中转站] 视频任务已提交，ID：%s", video_id)
        result = await poll_task(
            lambda: request_json(session, relay, "GET", f"/v1/videos/{video_id}", timeout=60), label="视频任务",
            task_id=video_id, **wait
        )
        url = find_video_url(result) or relay.url(f"/v1/videos/{video_id}/content")
        return await _download_video(session, relay, url, check_interrupt)


def _pick_artifact(data: Any) -> dict | None:
    """优先取视频。每个插件的产物 key 不一样，不能假定就叫 video。"""
    items = _first_value(data, ("artifacts",))
    items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    videos = [i for i in items if "video" in f"{i.get('type')} {i.get('mime_type')}".lower()]
    return (videos or items or [None])[0]


# 站点选渠道时就拒绝了：请求还没到插件，没有提交任务，也没有扣费，所以可以换个入口再提交一次
_NOT_SUBMITTED = ("task plugin not found", "无可用渠道", "no available channel", "model_not_found")


async def video_task(relay: Relay, plugin_key: str, body: dict, **wait: Any) -> tuple[bytes, str]:
    """new-api 的任务插件模型。默认提交到 /v1/videos，站点按模型找插件；填了插件键就提交到 /v1/tasks/{插件键}，
    键不对时再换 /v1/videos。之后轮询 GET /v1/tasks/{task_id}，成片在 artifacts 里：
    限定了模型的令牌查不了 /v1/video/generations 和 /v1/videos，这个接口不受影响。"""
    check_interrupt = wait["check_interrupt"]
    async with new_session() as session:
        async def submit(path: str) -> Any:
            return await guarded(request_json(session, relay, "POST", path, json_body=body), check_interrupt)

        try:
            created = await submit(f"/v1/tasks/{quote(plugin_key, safe='')}" if plugin_key else "/v1/videos")
        except RelayError as e:
            if not plugin_key or not any(word in str(e).lower() for word in _NOT_SUBMITTED):
                raise
            log.warning("[中转站] 任务插件「%s」用不了，改由站点按模型找插件：%s", plugin_key, e)
            created = await submit("/v1/videos")
        task_id = find_task_id(created)
        if find_status(created) in FAILURE_STATUSES or not task_id:
            raise RelayError(f"视频任务提交失败：{find_error_message(created) or short(created)}")
        log.info("[中转站] 视频任务已提交，任务 ID：%s", task_id)
        path = f"/v1/tasks/{quote(task_id, safe='')}"
        result = await poll_task(
            lambda: request_json(session, relay, "GET", path, timeout=60), label="视频任务", task_id=task_id, **wait
        )
        try:
            listing = await guarded(
                request_json(session, relay, "GET", f"{path}/artifacts", timeout=60), check_interrupt
            )
            # 产物里的 content_url 可能带访问凭据，用带令牌的内容接口下载，不让它出现在日志和节点输出里
            item = _pick_artifact(listing)
            if item and item.get("key"):
                url = relay.url(f"{path}/artifacts/{quote(str(item['key']), safe='')}/content")
            else:
                url = find_video_url(result)  # 插件没有实现产物接口
            if not url:
                raise RelayError("站点没有列出成片")
            return await _download_video(session, relay, url, check_interrupt)
        except RelayError as e:
            raise RelayError(f"任务已完成，但下载成片失败（任务 ID：{task_id}）：{e}") from None


async def video_chat(
    relay: Relay, body: dict, *, stream: bool, timeout: float, check_interrupt: CheckInterrupt
) -> tuple[bytes, str]:
    """走对话接口出视频：回复文字里带视频链接。"""
    text, message = await chat_completion(relay, body, timeout=timeout, check_interrupt=check_interrupt, stream=stream)
    url = find_video_url_in_text(text) or find_video_url_in_text(message.get("reasoning_content") or "")
    if not url:
        raise RelayError(f"回复里没有找到视频链接：{short(text) or '（空）'}")
    async with new_session() as session:
        return await _download_video(session, relay, url, check_interrupt)


async def video_custom(relay: Relay, submit_path: str, query_path: str, body: dict, **wait: Any) -> tuple[bytes, str]:
    """自定义路径：适配可灵/即梦等厂商原生格式。"""
    async with new_session() as session:
        created = await guarded(
            request_json(session, relay, "POST", submit_path, json_body=body), wait["check_interrupt"]
        )
        return await _finish_video_task(session, relay, created, query_path, **wait)
