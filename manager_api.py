"""中转站管理器的 HTTP 接口。

ComfyUI 里挂在 PromptServer.instance.routes 上；独立运行时由 manager_app.py 挂在自己的 aiohttp 服务上
（额外加一次性令牌）。两种模式都校验 Host：ComfyUI 自己的跨域检查挡不住 DNS 重绑定，
攻击网页可以把自己的域名解析到 127.0.0.1 来调用这里的接口。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import time

from aiohttp import web

try:
    from . import relay_client as rc
    from . import relay_probe as rp
    from . import relay_store as rs
except ImportError:  # 独立运行管理器时
    import relay_client as rc
    import relay_probe as rp
    import relay_store as rs

log = logging.getLogger("comfyui_relay_api")

PREFIX = "/relay_api"
MANAGER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manager")
# 页面用到的文件逐个列出来：比按扩展名放行更安全（Windows 上 C:x.js 这种盘符相对路径能逃出目录）
STATIC_FILES = {
    "index.html": "text/html",
    "app.js": "text/javascript",
    "style.css": "text/css",
    "icon.svg": "image/svg+xml",
    "icon.ico": "image/x-icon",
}
CHAT_TEST_PROMPT = "这是连通性测试，请只回复两个字：收到"
ALLOWED_HOSTS_ENV = "COMFY_RELAY_ALLOWED_HOSTS"  # 用域名访问 ComfyUI 时，把域名加到这里（逗号分隔）


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def host_allowed(request: web.Request) -> bool:
    """只认 IP 地址和 localhost（DNS 重绑定攻击用的一定是域名）。"""
    host = (request.host or "").strip().lower()
    if host.startswith("["):
        hostname = host[1:].split("]")[0]
    elif host.count(":") == 1:
        hostname = host.rsplit(":", 1)[0]
    else:
        hostname = host
    if hostname == "localhost":
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    extra = {h.strip().lower() for h in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",") if h.strip()}
    return hostname in extra


async def _body(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise rc.RelayError("请求格式不对（需要 JSON）")
    try:
        data = await request.json()
    except Exception:
        raise rc.RelayError("请求格式不对") from None
    if not isinstance(data, dict):
        raise rc.RelayError("请求格式不对")
    return data


def _handled(fn):
    """校验 Host，把 RelayError 转成给界面看的错误信息；意外错误也返回 JSON，不让界面只看到 HTTP 500。"""
    async def wrapper(request: web.Request) -> web.StreamResponse:
        if not host_allowed(request):
            return _error(f"中转站管理器只允许用 IP 或 localhost 访问（可用环境变量 {ALLOWED_HOSTS_ENV} 放行域名）", 403)
        try:
            return await fn(request)
        except rc.RelayError as e:
            return _error(str(e))
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("[中转站] 管理器接口出错：%s", request.path)
            return _error(f"内部错误：{type(e).__name__}: {rc.short(str(e), 300)}", 500)
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------- 中转站增删改 ----------

@_handled
async def get_state(request: web.Request) -> web.Response:
    relays = rs.list_relays()
    placeholders = [r["name"] for r in relays if r["placeholder"]]
    return web.json_response({"config_path": rc.config_path(), "relays": relays, "placeholders": placeholders})


@_handled
async def parse_text(request: web.Request) -> web.Response:
    data = await _body(request)
    existing = [r["name"] for r in rs.list_relays()]
    return web.json_response(rs.parse_quick_text(str(data.get("text") or ""), existing))


@_handled
async def create_relay(request: web.Request) -> web.Response:
    data = await _body(request)
    view = rs.add_relay(str(data.get("name") or ""), str(data.get("base_url") or ""),
                        str(data.get("api_key") or ""), str(data.get("proxy") or ""))
    rp.forget(view["name"])
    return web.json_response({"relay": view})


@_handled
async def update_relay(request: web.Request) -> web.Response:
    old_name = request.match_info["name"]
    data = await _body(request)
    view = rs.update_relay(old_name, str(data.get("name") or ""), str(data.get("base_url") or ""),
                           str(data.get("api_key") or ""), str(data.get("proxy") or ""))
    rp.forget(old_name)
    rp.forget(view["name"])
    return web.json_response({"relay": view})


@_handled
async def delete_relay(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    rs.delete_relay(name)
    rp.forget(name)
    return web.json_response({"ok": True})


@_handled
async def remove_placeholders(request: web.Request) -> web.Response:
    return web.json_response({"removed": rs.remove_placeholders()})


# ---------- 测试 / 模型 ----------

@_handled
async def test_relay(request: web.Request) -> web.Response:
    """测试表单里还没保存的配置。令牌留空时可以沿用已保存的令牌，但必须还是同一个站点、没换代理。"""
    data = await _body(request)
    api_key = str(data.get("api_key") or "").strip()
    base_url, proxy = str(data.get("base_url") or ""), str(data.get("proxy") or "")
    keep = data.get("keep_key_of")
    stored = rc.load_relays().get(str(keep)) if keep else None
    if keep and stored is None:
        raise rc.RelayError(f"没有名为「{keep}」的中转站")
    if stored is not None:  # 界面上显示的是遮挡过密码的地址，原样提交回来表示没改
        base_url = rs.unmask(base_url, str(stored.get("base_url") or ""))
        proxy = rs.unmask(proxy, str(stored.get("proxy") or ""))
    base_url, proxy = rs.check_base_url(base_url, api_key), rs.check_proxy(proxy)
    if not api_key and stored is not None:
        if not rs.can_reuse_key(stored, base_url, proxy):
            raise rc.RelayError(rs.KEY_REQUIRED_MSG)
        api_key = str(stored.get("api_key") or "").strip()
        if rc._unfilled(api_key) and stored.get("api_key_env"):
            api_key = os.environ.get(str(stored["api_key_env"]), "").strip()
    if not api_key or rc._unfilled(api_key):
        raise rc.RelayError("请填写令牌（API Key）")
    relay = rc.Relay(str(data.get("name") or "未保存的中转站"), base_url, api_key, proxy or None)
    return web.json_response(await rp.probe(relay, refresh=True))


@_handled
async def get_models(request: web.Request) -> web.Response:
    relay = rc.get_relay(request.match_info["name"])
    refresh = request.query.get("refresh") in ("1", "true")
    return web.json_response(await rp.probe(relay, refresh=refresh))


@_handled
async def chat_test(request: web.Request) -> web.Response:
    """给文本模型发一句话，确认真的能用（花费极少）。"""
    data = await _body(request)
    relay = rc.get_relay(str(data.get("name") or ""))
    model = str(data.get("model") or "").strip()
    if not model:
        raise rc.RelayError("缺少模型名")
    body = {"model": model, "messages": [{"role": "user", "content": CHAT_TEST_PROMPT}]}
    start = time.monotonic()
    text, _ = await rc.chat_completion(relay, body, timeout=90, check_interrupt=None)
    text = " ".join(text.split())
    return web.json_response({
        "reply": text if len(text) <= 120 else text[:120] + "…",
        "latency_ms": round((time.monotonic() - start) * 1000),
    })


# ---------- 页面 ----------

@_handled
async def manager_redirect(request: web.Request) -> web.Response:
    query = f"?{request.query_string}" if request.query_string else ""
    raise web.HTTPFound(f"{request.path}/{query}")


@_handled
async def manager_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info.get("file") or "index.html"
    if name not in STATIC_FILES:
        raise web.HTTPNotFound()
    return web.FileResponse(os.path.join(MANAGER_DIR, name),
                            headers={"Cache-Control": "no-cache", "Content-Type": STATIC_FILES[name]})


# ---------- 和 ComfyUI 画布联动 ----------

NODE_TYPES = {"RelayAPI_Chat", "RelayAPI_Image", "RelayAPI_Video"}
API_FORMATS = {"images", "images-json", "chat", "gemini", "unified", "openai", "custom", "task"}


def clean_event(data: dict) -> dict:
    """独立管理器发给 ComfyUI 页面的通知：中转站有变化，或者往画布上加一个节点。"""
    kind = data.get("type")
    if kind == "relays-changed":
        return {"type": kind}
    if kind != "add-node":
        raise rc.RelayError("不支持的通知类型")
    node, fmt, relay, model = (data.get(k) for k in ("node", "api_format", "relay", "model"))
    if not (isinstance(node, str) and node in NODE_TYPES and isinstance(relay, str) and isinstance(model, str)
            and relay and model and len(relay) <= 100 and len(model) <= 300
            and (fmt is None or (isinstance(fmt, str) and fmt in API_FORMATS))):
        raise rc.RelayError("节点信息不完整")
    return {"type": kind, "node": node, "relay": relay, "model": model, "api_format": fmt}


def add_comfy_routes(routes: web.RouteTableDef, send) -> None:
    """只在 ComfyUI 里注册：让独立管理器能找到正在运行的 ComfyUI，并通知它的页面。
    send(event) 返回收到通知的页面数量。"""

    @_handled
    async def ping(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "app": "comfyui"})

    @_handled
    async def notify(request: web.Request) -> web.Response:
        event = clean_event(await _body(request))
        receivers = send(event)
        if not receivers and event["type"] == "add-node":
            raise rc.RelayError("ComfyUI 正在运行，但没有打开的 ComfyUI 界面，打开后再试")
        return web.json_response({"ok": True, "receivers": receivers})

    routes.get(f"{PREFIX}/ping")(ping)
    routes.post(f"{PREFIX}/notify")(notify)


def add_routes(routes: web.RouteTableDef) -> None:
    """把管理器的接口注册到一个 RouteTableDef 上。"""
    routes.get(f"{PREFIX}/state")(get_state)
    routes.post(f"{PREFIX}/parse")(parse_text)
    routes.post(f"{PREFIX}/relays")(create_relay)
    routes.put(f"{PREFIX}/relays/{{name}}")(update_relay)
    routes.delete(f"{PREFIX}/relays/{{name}}")(delete_relay)
    routes.post(f"{PREFIX}/placeholders/remove")(remove_placeholders)
    routes.post(f"{PREFIX}/test")(test_relay)
    routes.get(f"{PREFIX}/models/{{name}}")(get_models)
    routes.post(f"{PREFIX}/chat_test")(chat_test)
    routes.get(f"{PREFIX}/manager")(manager_redirect)
    routes.get(f"{PREFIX}/manager/")(manager_file)
    routes.get(f"{PREFIX}/manager/{{file}}")(manager_file)
