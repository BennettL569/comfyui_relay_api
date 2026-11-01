"""relays.json 的读写：增删改、遮挡令牌、从粘贴的文字里识别接口地址和令牌。

ComfyUI 里作为插件子模块导入，独立管理器里作为顶层模块导入，所以两种导入方式都要支持。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse, urlunparse

try:
    from . import relay_client as rc
    from .relay_i18n import tr
except ImportError:  # 独立运行管理器时
    import relay_client as rc
    from relay_i18n import tr

NAME_MAX = 40
PANEL_EDIT_KEY = "_panel_edit"  # 以下划线开头：不算中转站，名字也不允许以下划线开头，所以接口没法借添加中转站改它
_THREAD_LOCK = threading.Lock()

KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_\-]{8,}")
LABELED_KEY_RE = re.compile(r"(?:key|令牌|密钥|token|apikey|api_key)\s*(?:[:：=]|为|是)?\s*([A-Za-z0-9_\-.]{20,})", re.I)
# 只认 ASCII 网址字符：粘在网址后面的中文、逗号、分号都不算网址，否则令牌会被吞进接口地址
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&*+=%\-]+", re.I)
BARE_HOST_RE = re.compile(r"(?<![@\w.-])((?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/v1)?)(?![\w@.-])", re.I)
BARE_PROXY_RE = re.compile(r"(?:代理|proxy)\S{0,4}\s*[:：=]?\s*((?:\d{1,3}\.){3}\d{1,3}:\d+|localhost:\d+)", re.I)
URL_TAIL = ".:!?"
ENDPOINT_SUFFIXES = (
    "/chat/completions", "/completions", "/images/generations", "/images/edits", "/video/generations",
    "/videos", "/models", "/responses", "/messages", "/embeddings",
)
NON_API_SEGMENTS = {"console", "panel", "token", "tokens", "login", "register", "docs", "doc", "pricing", "topup",
                    "log", "logs", "dashboard", "about"}
LOCAL_HOST_RE = re.compile(r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", re.I)
URL_LABEL_RE = re.compile(r"(接口|地址|base|endpoint|api|域名|url)\s*[:：=]?\s*$", re.I)
PROXY_LABEL_RE = re.compile(r"(代理|proxy)\S{0,4}\s*[:：=]?\s*$", re.I)
KEY_REQUIRED_MSG = "If you change the relay address or proxy you must enter the token again (so it is never sent somewhere else)"


# ---------- 读写 ----------

@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """读-改-写期间加锁：线程锁管同一进程，锁文件管 ComfyUI 和独立管理器两个进程。"""
    with _THREAD_LOCK:
        lock_path = rc.config_path() + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        with open(lock_path, "a+b") as f:
            deadline = time.monotonic() + 10
            while True:
                try:
                    _lock_file(f)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise rc.RelayError(tr("The config file is being changed by another program. Try again shortly")) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                _unlock_file(f)


if os.name == "nt":
    import msvcrt

    def _lock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_raw() -> dict[str, Any]:
    """读取完整的 relays.json（包括以 _ 开头的说明字段），保持原有顺序。"""
    return rc.read_config()


def write_raw(data: dict[str, Any]) -> None:
    """原子写入：先写临时文件再替换，旧文件备份为 relays.json.bak（里面也有令牌，不要外传）。"""
    path = rc.config_path()
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".relays-", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.isfile(path):
            with contextlib.suppress(OSError):
                shutil.copy(path, path + ".bak")  # 连权限一起复制：备份里也有令牌
        # ComfyUI 正好在读文件时，Windows 上替换会短暂失败，重试几次
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise rc.RelayError(tr("Save failed: relays.json is in use by another program. Try again shortly")) from None
                time.sleep(0.05)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _relays(data: dict[str, Any]) -> dict[str, dict]:
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def _has_key(cfg: dict) -> bool:
    return not rc._unfilled(str(cfg.get("api_key") or "").strip()) or bool(str(cfg.get("api_key_env") or "").strip())


def is_placeholder(cfg: dict) -> bool:
    """示例里没填过的占位中转站：地址和令牌都没填，也没配环境变量。"""
    return rc._unfilled(str(cfg.get("base_url") or "").strip()) and not _has_key(cfg)


# ---------- 展示 ----------

def mask_key(key: str) -> str:
    key = key or ""
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def mask_url_auth(url: str) -> str:
    """地址里的账号密码不显示：http://alice:secret@host:3128 → http://•••@host:3128。有的代理把令牌写在用户名里，所以用户名也遮。"""
    parsed = urlparse(url or "")
    if not parsed.username and not parsed.password:
        return url or ""
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"•••@{host}" + (f":{parsed.port}" if parsed.port else "")
    return urlunparse(parsed._replace(netloc=netloc))


def public_view(name: str, cfg: dict) -> dict:
    """给界面看的信息：不包含完整令牌和代理密码。"""
    key = str(cfg.get("api_key") or "").strip()
    env = str(cfg.get("api_key_env") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    filled = bool(key) and not rc._unfilled(key)
    return {
        "name": name,
        "base_url": mask_url_auth(base_url),
        "url_filled": not rc._unfilled(base_url),
        "proxy": mask_url_auth(str(cfg.get("proxy") or "").strip()),
        "has_key": filled or bool(env and os.environ.get(env)),
        "key_masked": mask_key(key) if filled else "",
        "api_key_env": env,
        "configured": filled or bool(env),
        "placeholder": is_placeholder(cfg),
    }


def list_relays() -> list[dict]:
    return [public_view(name, cfg) for name, cfg in _relays(read_raw()).items()]


# ---------- 校验 ----------

def normalize_base_url(url: str) -> str:
    """去掉空白、结尾的斜杠和误粘进来的具体接口路径，例如 /v1/chat/completions → /v1；
    没写协议时，本机和局域网地址补 http://，其他补 https://。"""
    url = (url or "").strip().strip("\"'`")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = ("http://" if LOCAL_HOST_RE.match(url) else "https://") + url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    changed = True
    while changed:
        changed = False
        for suffix in ENDPOINT_SUFFIXES:
            if path.lower().endswith(suffix):
                path = path[: -len(suffix)].rstrip("/")
                changed = True
    segments = [s for s in path.split("/") if s]
    if segments and segments[0].lower() in NON_API_SEGMENTS:  # 粘成了控制台、文档页的地址
        path = ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", "")).rstrip("/")


def check_base_url(value: str, api_key: str = "") -> str:
    value = normalize_base_url(value)
    if not value:
        raise rc.RelayError(tr("Please enter the API address"))
    if any(ord(c) > 127 for c in value) or KEY_RE.search(value) or (api_key and api_key in value):
        raise rc.RelayError(tr("The API address contains non-ASCII text or a token. Please check it (enter only the https://… part)"))
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise rc.RelayError(tr("Invalid API address: {value}", value=value))
    return value


def check_proxy(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:  # Clash、v2rayN 里常写成 127.0.0.1:7890
        value = "http://" + value
    parsed = urlparse(value)
    try:
        parsed.port
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise rc.RelayError(tr("Only HTTP proxies are supported. Use the HTTP or mixed port of Clash or v2rayN, for example http://127.0.0.1:7890"))
    return value.rstrip("/")


def _check_name(name: str, data: dict, old_name: str | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise rc.RelayError(tr("Please enter a name"))
    if len(name) > NAME_MAX:
        raise rc.RelayError(tr("Name too long (at most {count} characters)", count=NAME_MAX))
    if name.startswith("_"):
        raise rc.RelayError(tr("Names cannot start with an underscore"))
    if any(c in name for c in "/\\"):
        raise rc.RelayError(tr("Names cannot contain slashes"))
    if name != old_name and name in data:  # 和配置里任何字段（包括手写的备注）都不能重名
        if isinstance(data[name], dict):
            raise rc.RelayError(tr('A relay named "{name}" already exists', name=name))
        raise rc.RelayError(tr('"{name}" clashes with another field in the config file. Please choose a different name', name=name))
    return name


def _site(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(normalize_base_url(url))
    try:
        port = parsed.port
    except ValueError:
        port = None
    return parsed.scheme, (parsed.hostname or "").lower(), port


def unmask(submitted: str, stored: str) -> str:
    """界面上显示的是遮挡过密码的地址，原样提交回来就表示没改，换回真实值。"""
    stored = (stored or "").strip()
    submitted = (submitted or "").strip()
    if stored and submitted == mask_url_auth(stored):
        return stored
    return submitted


def can_reuse_key(stored: dict, base_url: str, proxy: str) -> bool:
    """令牌留空沿用旧令牌的前提：还是发给同一个站点（协议、域名、端口都没变），而且没有换成新代理。
    去掉代理没问题；换代理不行，因为 http 站点的令牌会经过代理明文传输。"""
    same_site = _site(base_url) == _site(str(stored.get("base_url") or ""))
    proxy = (proxy or "").strip()
    return same_site and (not proxy or proxy == str(stored.get("proxy") or "").strip())


# ---------- 增删改 ----------

def add_relay(name: str, base_url: str, api_key: str, proxy: str = "") -> dict:
    with _locked():
        data = read_raw()
        name = _check_name(name, data)
        api_key = (api_key or "").strip()
        base_url = check_base_url(base_url, api_key)
        if not api_key:
            raise rc.RelayError(tr("Please enter the token (API key)"))
        data[name] = {"base_url": base_url, "api_key": api_key, "proxy": check_proxy(proxy)}
        write_raw(data)
        return public_view(name, data[name])


def update_relay(old_name: str, name: str, base_url: str, api_key: str = "", proxy: str = "") -> dict:
    """修改中转站。令牌留空表示沿用原令牌，但只有还是同一个站点、没换代理时才允许。"""
    with _locked():
        data = read_raw()
        relays = _relays(data)
        if old_name not in relays:
            raise rc.RelayError(tr('No relay named "{name}"', name=old_name))
        old = relays[old_name]
        name = _check_name(name, data, old_name)
        api_key = (api_key or "").strip()
        base_url = check_base_url(unmask(base_url, str(old.get("base_url") or "")), api_key)
        proxy = check_proxy(unmask(proxy, str(old.get("proxy") or "")))
        entry = dict(old)
        if api_key:
            entry["api_key"] = api_key
            entry.pop("api_key_env", None)
        elif not _has_key(old):
            raise rc.RelayError(tr("Please enter the token (API key)"))
        elif not can_reuse_key(old, base_url, proxy):
            raise rc.RelayError(tr(KEY_REQUIRED_MSG))
        entry["base_url"] = base_url
        entry["proxy"] = proxy
        # 改名时保持原来的位置
        data = {(name if k == old_name else k): (entry if k == old_name else v) for k, v in data.items()}
        write_raw(data)
        return public_view(name, entry)


def delete_relay(name: str) -> None:
    with _locked():
        data = read_raw()
        if name not in _relays(data):
            raise rc.RelayError(tr('No relay named "{name}"', name=name))
        del data[name]
        write_raw(data)


def set_panel_edit(enabled: bool) -> None:
    """ComfyUI 侧边栏能不能改中转站的开关，存在 relays.json 里。只有独立管理器能调用这个，见 manager_api.can_edit。"""
    with _locked():
        data = read_raw()
        data.pop(PANEL_EDIT_KEY, None)
        if enabled:
            data[PANEL_EDIT_KEY] = True
        write_raw(data)


def remove_placeholders() -> list[str]:
    """删掉示例里没填过的占位中转站，返回删掉的名字。"""
    with _locked():
        data = read_raw()
        removed = [name for name, cfg in _relays(data).items() if is_placeholder(cfg)]
        if removed:
            for name in removed:
                del data[name]
            write_raw(data)
        return removed


# ---------- 粘贴识别 ----------

def _score_url(url: str, before: str) -> int:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    segments = [s for s in parsed.path.lower().split("/") if s]
    score = 0
    if "v1" in segments:
        score += 3
    if host.startswith(("api.", "api-")):
        score += 2
    if URL_LABEL_RE.search(before[-20:]):
        score += 2
    if segments and segments[0] in NON_API_SEGMENTS:
        score -= 2
    if host in ("127.0.0.1", "localhost"):
        score -= 3
    return score


def suggest_name(base_url: str, existing: dict | list | None = None) -> str:
    """用域名起个名字：api.closeai.com → closeai，xxx.com.cn → xxx。"""
    host = (urlparse(base_url).hostname or "").lower()
    labels = [p for p in host.split(".") if p]
    if re.fullmatch(r"[\d.]+", host) or len(labels) < 2:
        base = host
    else:
        if len(labels) >= 3 and labels[-2] in ("com", "net", "org", "gov", "edu", "co", "ac"):
            labels = labels[:-2]
        else:
            labels = labels[:-1]
        labels = [p for p in labels if p not in ("api", "www", "openai", "gateway", "relay")]
        base = labels[-1] if labels else host
    base = base[:NAME_MAX] or "relay"
    taken = set(existing or [])
    name, i = base, 2
    while name in taken:
        name = f"{base}{i}"
        i += 1
    return name


def parse_quick_text(text: str, existing: dict | list | None = None) -> dict:
    """从随手粘贴的文字里找出接口地址、令牌和代理，例如：
    「API 地址：https://api.xxx.com/v1  令牌：sk-xxxx」"""
    text = text or ""
    result: dict[str, str] = {"name": "", "base_url": "", "api_key": "", "proxy": ""}

    best, best_score = None, -99
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(URL_TAIL)
        before = text[: m.start()]
        if PROXY_LABEL_RE.search(before[-16:]):
            result["proxy"] = url
            continue
        score = _score_url(url, before)
        if score > best_score:
            best, best_score = url, score
    if best:
        result["base_url"] = normalize_base_url(best)
    else:
        # 没写 http 的域名，例如 api.xxx.com/v1（排除邮箱）
        for m in BARE_HOST_RE.finditer(text):
            if PROXY_LABEL_RE.search(text[: m.start()][-16:]):
                result["proxy"] = result["proxy"] or "http://" + m.group(1)
                continue
            result["base_url"] = normalize_base_url(m.group(1))
            break
    if not result["proxy"]:
        m = BARE_PROXY_RE.search(text)
        if m:
            result["proxy"] = "http://" + m.group(1)

    key = KEY_RE.search(text)
    if key:
        result["api_key"] = key.group(0)
    else:
        labeled = LABELED_KEY_RE.search(text)
        if labeled and not labeled.group(1).lower().startswith("http"):
            result["api_key"] = labeled.group(1)
    if result["api_key"] and result["api_key"] in result["base_url"]:
        result["base_url"] = ""  # 宁可让用户自己填，也不能把令牌当成地址存下来

    if result["base_url"]:
        result["name"] = suggest_name(result["base_url"], existing)
    return result
