"""中转站 API 客户端：读取 relays.json、发请求、解析各家不同的返回格式。

只依赖 aiohttp，不依赖 ComfyUI，方便单独测试。图片和视频一律以 bytes 进出。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

import aiohttp

try:
    from .relay_i18n import tr
except ImportError:  # 独立运行管理器时
    from relay_i18n import tr

log = logging.getLogger("comfyui_relay_api")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_ENV = "COMFY_RELAY_CONFIG"
ALLOW_PRIVATE_ENV = "COMFY_RELAY_ALLOW_PRIVATE_DOWNLOADS"  # 接口回复里的文件确实放在内网时，在这台电脑上设成 1
# relays.example.json 里的占位文字（旧版本的示例是中文，手上的配置可能还留着）
PLACEHOLDER_WORDS = ("你的", "填", "your-relay", "your-api-key")

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
                raise RelayError(tr("{path} is in use by another program. Try again shortly.", path=os.path.basename(path))) from None
            time.sleep(0.05)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RelayError(tr("Cannot detect the encoding of {path}. Save it as UTF-8 and try again.", path=os.path.basename(path)))


def read_config() -> dict[str, Any]:
    """读取完整的 relays.json（包括以 _ 开头的说明字段），保持原有顺序。"""
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(_read_config_text(path))
    except json.JSONDecodeError as e:
        raise RelayError(tr("{path} is not valid JSON: {error} (the last saved version is in relays.json.bak)", path=os.path.basename(path), error=e)) from None
    if not isinstance(data, dict):
        raise RelayError(tr("The top level of {path} must be a JSON object", path=os.path.basename(path)))
    return data


def load_relays() -> dict[str, dict]:
    return {name: cfg for name, cfg in read_config().items() if not name.startswith("_") and isinstance(cfg, dict)}


def _unfilled(value: str) -> bool:
    return not value or any(word in value for word in PLACEHOLDER_WORDS)


def _split_proxy(proxy: str | None) -> tuple[str | None, aiohttp.BasicAuth | None]:
    """把代理地址里的账号密码拆出来单独传：写在地址里的话，代理报错时 aiohttp 的错误信息会把密码带出来。"""
    if not proxy:
        return None, None
    parsed = urlparse(proxy)
    try:
        port = parsed.port
    except ValueError:
        return proxy, None
    if not parsed.username and not parsed.password:
        return proxy, None
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    clean = urlunparse(parsed._replace(netloc=host + (f":{port}" if port else "")))
    return clean, aiohttp.BasicAuth(unquote(parsed.username or ""), unquote(parsed.password or ""))


class Relay:
    def __init__(self, name: str, base_url: str, api_key: str, proxy: str | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.proxy = proxy
        proxy_url, proxy_auth = _split_proxy(proxy)
        self.proxy_args = {"proxy": proxy_url, "proxy_auth": proxy_auth}
        # 去掉结尾的 /v1，各接口路径自己带版本号（/v1/...、/v1beta/...）
        self.root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    def url(self, path: str) -> str:
        """拼出请求地址。完整网址只允许中转站自己的，否则令牌会被发到别的网站
        （例如别人分享的工作流在自定义路径里填了一个外部网址）。"""
        if path.startswith(("http://", "https://")):
            if not self.same_origin(path):
                raise RelayError(tr("Can only request the relay's own address, not {host} (so your token is never sent to another site)", host=urlparse(path).netloc))
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

    def scrub(self, text: str) -> str:
        """报错文字里不能出现令牌和地址里的密码：报错会显示在界面上，也可能被转发给别人看。"""
        secrets = [self.api_key, urlparse(self.proxy or "").username, urlparse(self.proxy or "").password,
                   urlparse(self.root).password]
        for secret in secrets:
            if secret and len(secret) >= 4:  # 太短的（比如代理用户名 a）一替换反而把报错弄乱
                text = text.replace(secret, "***")
        return text


def get_relay(name: str) -> Relay:
    cfg = load_relays().get(name)
    if cfg is None:
        raise RelayError(tr('No relay named "{name}" in relays.json', name=name))
    base_url = str(cfg.get("base_url") or "").strip()
    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key and cfg.get("api_key_env"):
        api_key = os.environ.get(str(cfg["api_key_env"]), "").strip()
    if _unfilled(base_url) or not base_url.startswith(("http://", "https://")):
        raise RelayError(tr('The base_url of relay "{name}" is not set. It should look like https://api.example.com/v1', name=name))
    if _unfilled(api_key):
        raise RelayError(tr('The api_key of relay "{name}" is not set', name=name))
    return Relay(name, base_url, api_key, str(cfg.get("proxy") or "").strip() or None)


# ---------- 请求体与文本工具 ----------

def parse_extra(text: str | None) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RelayError(tr("Extra params are not valid JSON: {error}", error=e)) from None
    if not isinstance(data, dict):
        raise RelayError(tr('Extra params must be a JSON object, for example {"quality": "high"}'))
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
        raise RelayError(tr("Data URLs that are not base64 are not supported"))
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
    text = MD_DATA_IMAGE_RE.sub(tr("[image]"), text)
    return DATA_URL_RE.sub(tr("[image]"), text).strip()


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
MAX_IMAGE_REFS = 16  # 一条回复里最多下载这么多张图：回复内容不可信，每张都可能很大
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)


def _scrub_errors(fn):
    """第二个参数是 relay 的函数用这个：抛出的错误里去掉令牌和密码。"""
    @functools.wraps(fn)
    async def wrapper(session, relay, *args, **kwargs):
        try:
            return await fn(session, relay, *args, **kwargs)
        except RelayError as e:
            raise RelayError(relay.scrub(str(e))) from None
    return wrapper


async def _read_limited(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    """读响应体但设上限，防止恶意站点返回超大内容把内存撑爆。"""
    if resp.content_length is not None and resp.content_length > limit:
        raise RelayError(tr("Response too large ({size} MB), giving up", size=f"{resp.content_length / 1048576:.0f}"))
    chunks, size = [], 0
    async for chunk in resp.content.iter_chunked(1 << 16):
        size += len(chunk)
        if size > limit:
            raise RelayError(tr("Response too large (over {size} MB), giving up", size=f"{limit / 1048576:.0f}"))
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
        return tr("The relay returned a web page instead of API data{title}. The address may point to the site's "
                  "home page, or Cloudflare may be blocking the request (try setting a proxy).",
                  title=tr(" (page title: {title})", title=title) if title else "")
    try:
        data = json.loads(text)
    except ValueError:
        return short(text)
    # JSON 错误只留站点的原话：有的站把中文转义成 \uXXXX，原样显示看不懂
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str) and error["message"]:
        code = error.get("code")
        return short(f"{error['message']} ({code})" if code and isinstance(code, str) else error["message"])
    if isinstance(error, str) and error:
        return short(error)
    return short(data)


def _follow_same_origin(relay: Relay, method: str, url: str, location: str, resend: bool = False) -> str:
    """带令牌的请求不让 aiohttp 自动跟随重定向：它只会去掉 Authorization，别的头（比如 x-goog-api-key）照样带到新地址。
    只跟随同一个站点内的重定向，要去别的站点就报错，令牌不离开这个站。POST 只有 resend（307、308，请求体能再发一次）才跟。"""
    target = urljoin(url, location)
    if not (method == "GET" or resend) or not relay.same_origin(target):
        raise RelayError(tr("The relay redirected this request to {host}. Redirects are not followed so your token "
                            "stays on this relay. Please check the API address", host=urlparse(target).netloc))
    return target


@_scrub_errors
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
        for _ in range(MAX_REDIRECTS + 1):
            async with session.request(
                method, url, json=json_body, data=form, headers=relay.headers(headers) if auth else headers,
                allow_redirects=False, timeout=_timeout(timeout), **relay.proxy_args,
            ) as resp:
                status, ctype = resp.status, resp.content_type
                location = resp.headers.get("Location") if status in REDIRECT_CODES else None
                if not location:
                    text = _decode(await _read_limited(resp, max_bytes), resp.charset)
                    break
            url = _follow_same_origin(relay, method, url, location, resend=status in (307, 308) and form is None)
        else:
            raise RelayError(tr("Too many redirects: {url}", url=url))
    except TimeoutError:
        raise RelayError(tr("Request timed out ({seconds} s): {method} {url}", seconds=f"{timeout:g}", method=method, url=url)) from None
    except aiohttp.ClientError as e:
        raise RelayError(tr('Cannot reach relay "{name}": {error}', name=relay.name, error=e)) from None
    if status >= 400:
        raise RelayError(tr("{method} {url} returned HTTP {status}: {detail}", method=method, url=url, status=status, detail=describe_body(text, ctype)))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RelayError(tr("{method} {url} did not return JSON: {detail}", method=method, url=url, detail=describe_body(text, ctype))) from None


def raise_for_error_payload(data: Any, what: str) -> None:
    """有的站出错也回 HTTP 200，把错误放在 body 的 error 字段里。"""
    if not isinstance(data, dict) or not data.get("error"):
        return
    err = data["error"]
    message = (err.get("message") or err.get("msg")) if isinstance(err, dict) else err
    raise RelayError(tr("{what} failed: {detail}", what=what, detail=short(message or err)))


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


# Clash、sing-box 等的 TUN 假 IP 模式下，所有域名都解析到这些地址，真实地址由代理决定，不能当成内网
FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("fc00::/18"))
# 这些 IPv6 写法的低 32 位是一个 IPv4 地址：NAT64 网络里能连到内网，IPv4 兼容地址可能指向本机
EMBEDS_IPV4 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::/96"))


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        if any(ip in network for network in EMBEDS_IPV4):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        else:
            ip = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None) or ip
    if any(ip.version == network.version and ip in network for network in FAKE_IP_NETWORKS):
        return True
    return ip.is_global and not ip.is_multicast and not getattr(ip, "is_site_local", False)


def _private_download_error(host: str) -> RelayError:
    return RelayError(tr("Refusing to download from {host}: it points to a private address. To allow this, set "
                         "{env}=1 on this computer", host=host, env=ALLOW_PRIVATE_ENV))


async def _check_public(url: str) -> None:
    """接口回复里的下载地址不可信：别人分享的工作流可以让模型回复一个内网地址，插件下载后内容就被读出来了。
    不是中转站自己的地址时，不许指向本机和内网。"""
    if os.environ.get(ALLOW_PRIVATE_ENV) == "1":
        return
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or not host:
        raise RelayError(tr("Download failed: {url}: {error}", url=url, error=parsed.scheme or "?"))
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return  # 这里解析不了，可能要靠代理解析，真正连接时再说
    if any(not _is_public_ip(info[4][0]) for info in infos):
        raise _private_download_error(host)


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """连接时再查一次：防止域名检查时解析到公网，真正连接时又换成内网地址（DNS 重绑定）。"""

    def __init__(self) -> None:
        self._inner = aiohttp.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await self._inner.resolve(host, port, family)
        if os.environ.get(ALLOW_PRIVATE_ENV) != "1" and any(not _is_public_ip(r["host"]) for r in results):
            raise _private_download_error(host)
        return results

    async def close(self) -> None:
        await self._inner.close()


def _external_session(relay: Relay) -> aiohttp.ClientSession:
    """下载不是中转站的地址时用。走代理时域名由代理解析，这里看不到真实地址，只能靠连接前的检查。"""
    proxies = urllib.request.getproxies()  # 只设了 NO_PROXY 时这里也不是空的，所以只看 http 和 https
    if relay.proxy or proxies.get("http") or proxies.get("https"):
        return new_session()
    return aiohttp.ClientSession(trust_env=True, connector=aiohttp.TCPConnector(resolver=_PublicResolver()))


@_scrub_errors
async def download(session: aiohttp.ClientSession, relay: Relay, url: str, *, timeout: float = 600.0) -> bytes:
    if url.startswith("data:"):
        return decode_data_url(url)
    external = None
    try:
        url = relay.resolve(url)
        for _ in range(MAX_REDIRECTS + 1):
            own = relay.same_origin(url)  # 每一跳都重新判断：重定向可能指向任何地方
            if not own:
                await _check_public(url)
                external = external or _external_session(relay)
            # 只有和中转站同源的地址才带 Key，避免把 Key 发给第三方存储
            async with (session if own else external).get(
                url, headers=relay.headers() if own else None, allow_redirects=False,
                timeout=_timeout(timeout), **relay.proxy_args,
            ) as resp:
                status, ctype = resp.status, resp.content_type
                location = resp.headers.get("Location") if status in REDIRECT_CODES else None
                if not location:
                    body = await _read_limited(resp, MAX_DOWNLOAD)
                    break
            url = urljoin(url, location)
        else:
            raise RelayError(tr("Too many redirects: {url}", url=url))
    except TimeoutError:
        raise RelayError(tr("Download timed out: {url}", url=url)) from None
    except (aiohttp.ClientError, OSError, ValueError) as e:  # 回复里的地址可能是任何东西，ValueError 是地址写法不对
        raise RelayError(tr("Download failed: {url}: {error}", url=url, error=e)) from None
    finally:
        if external:
            await external.close()
    # 不是中转站的地址只报状态：把内容写进报错，等于让人能读到那个地址返回了什么
    if status >= 400:
        if not own:
            raise RelayError(tr("Download of {url} returned HTTP {status}", url=url, status=status))
        raise RelayError(tr("Download of {url} returned HTTP {status}: {detail}", url=url, status=status, detail=describe_body(_decode(body[:200_000], None), ctype)))
    if (ctype == "application/json" or ctype.startswith("text/")) and len(body) < 100_000:
        if not own:
            raise RelayError(tr("Download of {url} did not return a file", url=url))
        raise RelayError(tr("Download of {url} did not return a file: {detail}", url=url, detail=describe_body(_decode(body, None), ctype)))
    return body


# ---------- 对话接口 ----------

def _first_message(data: Any) -> dict:
    try:
        message = data["choices"][0]["message"] or {}
    except (KeyError, IndexError, TypeError):
        raise RelayError(tr("No choices[0].message in the response: {detail}", detail=short(data))) from None
    if not isinstance(message, dict):
        raise RelayError(tr("Unexpected message format in the response: {detail}", detail=short(data)))
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
            raise RelayError(tr("Response too large, giving up"))
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


@_scrub_errors
async def _chat_stream(session: aiohttp.ClientSession, relay: Relay, body: dict, timeout: float) -> tuple[str, dict]:
    url = relay.url("/v1/chat/completions")
    content, reasoning, images = [], [], []
    try:
        async with session.post(url, json=body, headers=relay.headers(), allow_redirects=False, timeout=_timeout(timeout), **relay.proxy_args) as resp:
            if resp.status in REDIRECT_CODES:
                _follow_same_origin(relay, "POST", url, resp.headers.get("Location") or "")
            if resp.status >= 400:
                text = _decode(await _read_limited(resp, 1 << 20), resp.charset)
                raise RelayError(tr("{method} {url} returned HTTP {status}: {detail}", method="POST", url=url, status=resp.status, detail=describe_body(text, resp.content_type)))
            if resp.content_type != "text/event-stream":
                # 有的站不管 stream 参数，直接一次性返回
                text = _decode(await _read_limited(resp, MAX_RESPONSE), resp.charset)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    raise RelayError(tr("{method} {url} did not return JSON: {detail}", method="POST", url=url, detail=describe_body(text, resp.content_type))) from None
                raise_for_error_payload(data, tr("Chat request"))
                message = _first_message(data)
                return message_text(message), message
            async for event in _iter_sse(resp):
                raise_for_error_payload(event, tr("Chat request"))
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
        raise RelayError(tr("Request timed out ({seconds} s): {method} {url}", seconds=f"{timeout:g}", method="POST", url=url)) from None
    except aiohttp.ClientError as e:
        raise RelayError(tr('Cannot reach relay "{name}": {error}', name=relay.name, error=e)) from None
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
    raise_for_error_payload(data, tr("Chat request"))
    message = _first_message(data)
    return message_text(message), message


async def list_models(relay: Relay, *, timeout: float = 60.0) -> list[str]:
    async with new_session() as session:
        data = await request_json(session, relay, "GET", "/v1/models", timeout=timeout)
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RelayError(tr("Cannot understand the model list format: {detail}", detail=short(data)))
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
    return list(dict.fromkeys(refs))[:MAX_IMAGE_REFS]


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
        raise_for_error_payload(data, tr("Image request"))
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
        raise RelayError(tr("No image in the response: {detail}", detail=short(data)))
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
        raise RelayError(tr("The model returned no image. Reply: {detail}", detail=short(text) or tr("(empty)")))
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
    path = f"/v1beta/models/{quote(model.removeprefix('models/'), safe='')}:generateContent"  # 模型名来自工作流，不能改变路径
    async with new_session() as session:
        data = await guarded(
            request_json(session, relay, "POST", path, json_body=body,
                         headers={"x-goog-api-key": relay.api_key}, timeout=timeout),
            check_interrupt,
        )
    raise_for_error_payload(data, tr("Gemini request"))
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
        raise RelayError(tr("Gemini returned no image ({reason}). Reply: {detail}", reason=reason or tr("unknown reason"), detail=short(''.join(texts)) or tr("(empty)")))
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
    tag = tr(" (task ID: {task_id})", task_id=task_id) if task_id else ""
    while True:
        try:
            data = await guarded(fetch(), check_interrupt)
        except RelayError as e:
            failures += 1
            if failures >= MAX_POLL_FAILURES or loop.time() >= deadline:
                raise RelayError(tr("{label} status check failed{tag}. The task may still be running, check the relay's console: {error}", label=label, tag=tag, error=e)) from None
            log.warning("[relay] %s status check failed (attempt %d): %s", label, failures, e)
            if on_status:
                on_status(tr("{label}: status check failed (attempt {count}), retrying…", label=label, count=failures))
            await sleep_checked(poll_interval, check_interrupt)
            continue
        failures = 0
        status = find_status(data)
        if status != last_status:
            log.info("[relay] %s status: %s  %s", label, status, short(data, 300))
            last_status = status
        progress = find_progress(data)
        if on_progress and progress is not None:
            on_progress(progress)
        if on_status:
            percent = f" {progress:.0f}%" if progress is not None else ""
            on_status(tr("{label}: {status}{percent}, waited {seconds} s", label=label, status=status or tr("processing"), percent=percent, seconds=f"{loop.time() - start:.0f}"))
        if status in SUCCESS_STATUSES or (status is None and find_video_url(data)):
            return data
        if status in FAILURE_STATUSES:
            raise RelayError(tr("{label} failed{tag}: {detail}", label=label, tag=tag, detail=find_error_message(data) or short(data)))
        if loop.time() >= deadline:
            raise RelayError(tr("{label} timed out ({seconds} s){tag}. Last status: {status}", label=label, seconds=f"{timeout:g}", tag=tag, status=status or tr("unknown")))
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
        raise RelayError(tr("Video task submission failed: {detail}", detail=find_error_message(created) or short(created)))
    task_id = find_task_id(created)
    url = find_video_url(created) if (status in SUCCESS_STATUSES or not task_id or not query_path) else None
    if not url:
        if not task_id or not query_path:
            raise RelayError(tr("No task ID or video address after submitting. Response: {detail}", detail=short(created)))
        log.info("[relay] video task submitted, task ID: %s", task_id)
        path = query_path.replace("{task_id}", task_id).replace("{id}", task_id)
        result = await poll_task(
            lambda: request_json(session, relay, "GET", path, timeout=60), label=tr("Video task"), task_id=task_id, **wait
        )
        url = find_video_url(result)
        if not url:
            raise RelayError(tr("Task finished, but no video address in the response (task ID: {task_id}): {detail}", task_id=task_id, detail=short(result)))
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
            raise RelayError(tr("Video task submission failed: {detail}", detail=find_error_message(created) or short(created)))
        log.info("[relay] video task submitted, ID: %s", video_id)
        result = await poll_task(
            lambda: request_json(session, relay, "GET", f"/v1/videos/{video_id}", timeout=60), label=tr("Video task"),
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
            log.warning("[relay] task plugin %r is not usable, letting the relay pick by model: %s", plugin_key, e)
            created = await submit("/v1/videos")
        task_id = find_task_id(created)
        if find_status(created) in FAILURE_STATUSES or not task_id:
            raise RelayError(tr("Video task submission failed: {detail}", detail=find_error_message(created) or short(created)))
        log.info("[relay] video task submitted, task ID: %s", task_id)
        path = f"/v1/tasks/{quote(task_id, safe='')}"
        result = await poll_task(
            lambda: request_json(session, relay, "GET", path, timeout=60), label=tr("Video task"), task_id=task_id, **wait
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
                raise RelayError(tr("The relay listed no finished video"))
            return await _download_video(session, relay, url, check_interrupt)
        except RelayError as e:
            raise RelayError(tr("Task finished, but downloading the video failed (task ID: {task_id}): {error}", task_id=task_id, error=e)) from None


async def video_chat(
    relay: Relay, body: dict, *, stream: bool, timeout: float, check_interrupt: CheckInterrupt
) -> tuple[bytes, str]:
    """走对话接口出视频：回复文字里带视频链接。"""
    text, message = await chat_completion(relay, body, timeout=timeout, check_interrupt=check_interrupt, stream=stream)
    url = find_video_url_in_text(text) or find_video_url_in_text(message.get("reasoning_content") or "")
    if not url:
        raise RelayError(tr("No video link found in the reply: {detail}", detail=short(text) or tr("(empty)")))
    async with new_session() as session:
        return await _download_video(session, relay, url, check_interrupt)


async def video_custom(relay: Relay, submit_path: str, query_path: str, body: dict, **wait: Any) -> tuple[bytes, str]:
    """自定义路径：适配可灵/即梦等厂商原生格式。"""
    async with new_session() as session:
        created = await guarded(
            request_json(session, relay, "POST", submit_path, json_body=body), wait["check_interrupt"]
        )
        return await _finish_video_task(session, relay, created, query_path, **wait)
