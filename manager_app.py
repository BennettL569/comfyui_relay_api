"""中转站管理器独立版：不用打开 ComfyUI 就能添加、测试中转站。用 tools 目录里的启动脚本或桌面快捷方式打开。

启动一个只监听 127.0.0.1 的本机服务（首选端口 18766 + 一次性令牌），用 Edge/Chrome 的应用窗口打开管理页面。
页面通过 WebSocket 和服务保持连接，窗口关掉后服务自动退出。ComfyUI 开着的话，修改会同步过去。

    pythonw manager_app.py            # 正常使用（没有黑色命令行窗口）
    python  manager_app.py --no-window --port 8899   # 只启动服务，便于调试
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # 以顶层模块方式导入插件里的纯 Python 模块（不依赖 ComfyUI）

import aiohttp  # noqa: E402
from aiohttp import WSMsgType, web  # noqa: E402

import manager_api  # noqa: E402
import relay_client as rc  # noqa: E402

log = logging.getLogger("comfyui_relay_api.manager")

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/state"), "comfyui_relay_api")
INSTANCE_FILE = os.path.join(DATA_DIR, "instance.json")
PREFERRED_PORT = 18766  # 固定端口让窗口位置、大小这些浏览器记忆保持不变；被占用会自动换
FIRST_CONNECT_TIMEOUT = 120  # 秒：启动后（或又要开一个窗口时）这么久都没有窗口连上就退出
CLOSE_GRACE = 6  # 秒：窗口断开后等这么久（刷新页面会短暂断开）
COMFY_RECHECK = 10  # 秒
MUTEX_NAME = "Local\\comfyui_relay_api.manager"
BROWSER_PATHS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]


def _code_stamp() -> str:
    """这份插件代码的指纹。进程启动时算一次，之后文件改了它也不变，所以能看出谁还在跑旧代码。"""
    parts = [f"{entry.name}:{entry.stat().st_size}:{entry.stat().st_mtime_ns}"
             for entry in os.scandir(HERE) if entry.name.endswith(".py")]
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:16]


CODE_STAMP = _code_stamp()


def _comfy_ports() -> list[int]:
    """ComfyUI 默认 8188，被占用时往后顺延；用了别的端口可以设环境变量，如 COMFY_RELAY_COMFY_PORTS=8000,8188-8190。
    写错的部分直接跳过，不能让管理器因为一个环境变量打不开。"""
    ports: list[int] = []
    for part in os.environ.get("COMFY_RELAY_COMFY_PORTS", "8188-8197").split(","):
        lo, _, hi = (s.strip() for s in part.partition("-"))
        if lo.isdigit() and (not hi or hi.isdigit()):
            ports.extend(p for p in range(int(lo), int(hi or lo) + 1) if 0 < p < 65536)
    return ports[:50] or list(range(8188, 8198))


class ComfyLink:
    """找到本机正在运行（并装了本插件）的 ComfyUI，把通知转发给它的页面。"""

    def __init__(self, ports=None):
        self.ports = list(ports) if ports is not None else _comfy_ports()
        self.port: int | None = None
        self.checked = -COMFY_RECHECK
        self._session: aiohttp.ClientSession | None = None

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env=False：访问本机不要走系统代理
            self._session = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=5))
        return self._session

    async def _check(self, port: int) -> int | None:
        try:
            async with self._client().get(f"http://127.0.0.1:{port}{manager_api.PREFIX}/ping",
                                          timeout=aiohttp.ClientTimeout(total=1.5)) as r:
                data = await r.json(content_type=None)
                return port if r.status == 200 and isinstance(data, dict) and data.get("app") == "comfyui" else None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    async def find(self, refresh: bool = False) -> int | None:
        if refresh or time.monotonic() - self.checked > COMFY_RECHECK:
            found = await asyncio.gather(*(self._check(p) for p in self.ports))
            self.port = next((p for p in found if p), None)
            self.checked = time.monotonic()
        return self.port

    async def notify(self, event: dict) -> None:
        port = await self.find() or await self.find(refresh=True)
        if not port:
            raise rc.RelayError("没有检测到正在运行的 ComfyUI（需要先启动 ComfyUI）")
        try:
            async with self._client().post(f"http://127.0.0.1:{port}{manager_api.PREFIX}/notify", json=event) as r:
                if r.status != 200:
                    try:
                        data = await r.json(content_type=None)
                    except ValueError:
                        data = None
                    message = data.get("error") if isinstance(data, dict) else None
                    raise rc.RelayError(message or f"ComfyUI 返回 HTTP {r.status}")
        except (aiohttp.ClientError, TimeoutError) as e:
            self.port = None
            raise rc.RelayError(f"连不上 ComfyUI：{e}") from None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class ManagerServer:
    """独立模式的服务：在 ComfyUI 的同一套接口外面加上 Host 校验和令牌校验。"""

    def __init__(self, port: int = 0, token: str | None = None, comfy_ports=None):
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.connections = 0
        self.ever_connected = False
        self.last_disconnect = time.monotonic()
        self.expect_until = 0.0  # 又要开一个窗口时，在这之前不退出
        self.code = CODE_STAMP
        self.quitting = False
        self.comfy = ComfyLink(comfy_ports)
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{manager_api.PREFIX}/manager/?token={self.token}"

    def token_ok(self, token: str) -> bool:
        # 按字节比较：令牌里混进非 ASCII 字符时 compare_digest 对 str 会抛异常
        return secrets.compare_digest(token.encode("utf-8", "surrogateescape"), self.token.encode())

    @web.middleware
    async def guard(self, request: web.Request, handler):
        # 只认本机地址，挡住 DNS 重绑定（别的网站把自己的域名解析到 127.0.0.1 来访问这里）
        host = (request.host or "").lower()
        hostname = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            return web.json_response({"error": "只允许本机访问"}, status=403)
        # 页面和样式不含敏感信息，其余接口都要带令牌
        if not request.path.startswith(f"{manager_api.PREFIX}/manager"):
            token = request.headers.get("X-Relay-Token") or request.query.get("token") or ""
            if not self.token_ok(token):
                return web.json_response({"error": "令牌不对，请重新打开管理器"}, status=403)
        return await handler(request)

    async def live(self, request: web.Request) -> web.WebSocketResponse:
        """页面打开期间一直连着；全部断开一段时间后服务退出。"""
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.connections += 1
        self.ever_connected = True
        self.expect_until = 0.0
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.connections -= 1
            self.last_disconnect = time.monotonic()
        return ws

    async def hello(self, request: web.Request) -> web.Response:
        """再次双击快捷方式时用：确认是本管理器在运行。代码没变就约好马上会有新窗口连上来，先别退出。"""
        if request.query.get("code") == self.code:
            self.expect_until = time.monotonic() + FIRST_CONNECT_TIMEOUT
        return web.json_response({"app": "relay-manager", "pid": os.getpid(), "code": self.code})

    async def quit(self, request: web.Request) -> web.Response:
        """插件更新后，新启动的管理器让还在跑旧代码的后台退出。"""
        self.quitting = True
        return web.json_response({"ok": True})

    async def comfy_status(self, request: web.Request) -> web.Response:
        port = await self.comfy.find(refresh=request.query.get("refresh") == "1")
        return web.json_response({"running": bool(port), "port": port})

    async def comfy_notify(self, request: web.Request) -> web.Response:
        try:
            await self.comfy.notify(manager_api.clean_event(await manager_api._body(request)))
        except rc.RelayError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def start(self) -> None:
        app = web.Application(middlewares=[self.guard])
        routes = web.RouteTableDef()
        manager_api.add_routes(routes)
        routes.get(f"{manager_api.PREFIX}/live")(self.live)
        routes.get(f"{manager_api.PREFIX}/hello")(self.hello)
        routes.post(f"{manager_api.PREFIX}/quit")(self.quit)
        routes.get(f"{manager_api.PREFIX}/comfy/status")(self.comfy_status)
        routes.post(f"{manager_api.PREFIX}/comfy/notify")(self.comfy_notify)
        app.add_routes(routes)
        # shutdown_timeout 默认 60 秒，窗口关了进程还会挂一分钟
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=2.0)
        await self._runner.setup()
        sock = self._bind(self.port)
        self.port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()

    @staticmethod
    def _bind(port: int) -> socket.socket:
        """独占端口：Windows 上别的程序监听 0.0.0.0 同一端口时，不加这个也能绑定成功，会抢走它的本机连接。"""
        def make() -> socket.socket:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            return sock

        sock = make()
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            sock = make()
            sock.bind(("127.0.0.1", 0))  # 首选端口被占用就换一个
        return sock

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        await self.comfy.close()

    def should_exit(self, started: float) -> bool:
        if self.quitting:
            return True
        now = time.monotonic()
        if self.connections:
            return False
        if now < self.expect_until:
            return False
        if not self.ever_connected:
            return now - started > FIRST_CONNECT_TIMEOUT
        return now - self.last_disconnect > CLOSE_GRACE


def find_browser() -> str | None:
    for raw in BROWSER_PATHS:
        path = os.path.expandvars(raw)
        if "%" not in path and os.path.isfile(path):
            return path
    return None


def open_window(url: str) -> None:
    """优先用 Edge/Chrome 的应用窗口（像独立软件一样没有地址栏），找不到就用默认浏览器。"""
    browser = find_browser()
    if browser:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.Popen([browser, f"--app={url}", "--window-size=1180,820", "--no-first-run",
                          "--no-default-browser-check"], creationflags=flags)
        log.info("用 %s 打开管理器窗口", os.path.basename(browser))
    else:
        webbrowser.open(url)
        log.info("没找到 Edge/Chrome，用默认浏览器打开")


# ---------- 单实例 ----------

def _read_instance() -> dict | None:
    try:
        with open(INSTANCE_FILE, encoding="utf-8") as f:
            info = json.load(f)
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


_RETIRED = "retired"


async def _retire(session: aiohttp.ClientSession, base: str, headers: dict) -> None:
    """让还在跑旧代码的后台退出，等它让出端口。没有这个接口的旧版本就留到窗口关掉，新后台会换个端口。"""
    try:
        async with session.post(f"{base}/quit", headers=headers) as r:
            if r.status != 200:
                return
        for _ in range(20):
            await asyncio.sleep(0.25)
            async with session.get(f"{base}/hello", headers=headers):
                pass
    except (aiohttp.ClientError, TimeoutError):
        pass  # 连不上了，说明已经退出


async def _running_instance() -> str | None:
    """已经开着一个管理器的话，返回它的地址（再双击快捷方式时直接再开一个窗口，不重复启动服务）。
    它跑的还是更新前的代码就不能接着用，不然重新打开看到的还是旧结果：让它退出，返回 _RETIRED。"""
    info = _read_instance()
    if not info:
        return None
    try:
        port, token, pid = int(info["port"]), str(info["token"]), int(info["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    base = f"http://127.0.0.1:{port}{manager_api.PREFIX}"
    headers = {"X-Relay-Token": token}
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=2)) as s:
            async with s.get(f"{base}/hello", params={"code": CODE_STAMP}, headers=headers) as r:
                data = await r.json(content_type=None) if r.status == 200 else None
            if isinstance(data, dict) and data.get("app") == "relay-manager" and data.get("pid") == pid:
                if data.get("code") == CODE_STAMP:
                    return f"{base}/manager/?token={token}"
                log.info("管理器后台还在跑更新前的代码，换一个新的")
                await _retire(s, base, headers)
                return _RETIRED
    except (aiohttp.ClientError, TimeoutError, ValueError):
        pass
    _remove_instance(pid)  # 上次没正常退出留下的旧记录
    return None


def _write_instance(server: ManagerServer) -> None:
    # 里面有令牌：Linux、macOS 上同一台机器的其他用户不能读
    fd = os.open(INSTANCE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "port": server.port, "token": server.token}, f)


def _remove_instance(pid: int) -> None:
    info = _read_instance()  # 先读完关掉文件再删：Windows 上不能删除打开着的文件
    if info and info.get("pid") == pid:
        try:
            os.remove(INSTANCE_FILE)
        except OSError:
            pass


class _SingleInstance:
    """命名互斥量：两次快速双击时，第二个进程等第一个启动好，再复用它，而不是起两个服务。"""

    def __init__(self) -> None:
        self.handle = None
        self.first = True
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
            self.first = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _quiet_connection_reset(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    # 窗口关闭时浏览器直接断开连接，Windows 的事件循环会把这当成错误打一大段日志，其实无害
    if isinstance(context.get("exception"), ConnectionResetError):
        return
    loop.default_exception_handler(context)


async def run(port: int, show_window: bool) -> None:
    asyncio.get_running_loop().set_exception_handler(_quiet_connection_reset)
    if show_window:
        lock = _SingleInstance()
        deadline = time.monotonic() + (10 if not lock.first else 0)
        while True:
            existing = await _running_instance()
            if existing == _RETIRED:
                break
            if existing:
                log.info("管理器已经在运行，再打开一个窗口")
                open_window(existing)
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.3)  # 另一个进程正在启动，等它写好 instance.json
    server = ManagerServer(port=port)
    await server.start()
    log.info("管理器已启动：http://127.0.0.1:%s%s/manager/", server.port, manager_api.PREFIX)
    if show_window:
        _write_instance(server)
        open_window(server.url)
    elif sys.stdout:
        print(server.url, flush=True)
    started = time.monotonic()
    try:
        while not (show_window and server.should_exit(started)):
            await asyncio.sleep(1)
    finally:
        if show_window:
            _remove_instance(os.getpid())
        await server.stop()
        log.info("管理器窗口已关闭，服务退出")


def _show_fatal(message: str) -> None:
    """pythonw 没有控制台，出错时弹个对话框，不然用户只会看到「点了没反应」。"""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "中转站管理器", 0x10)


def main() -> None:
    parser = argparse.ArgumentParser(description="中转站管理器")
    parser.add_argument("--port", type=int, default=PREFERRED_PORT, help=f"监听端口，默认 {PREFERRED_PORT}，被占用会自动换")
    parser.add_argument("--no-window", action="store_true", help="只启动服务，不打开窗口（调试用）")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    log_path = os.path.join(DATA_DIR, "manager.log")
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(log_path, maxBytes=1 << 20, backupCount=1, encoding="utf-8"),
    ]
    if sys.stderr:  # pythonw 没有控制台
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        asyncio.run(run(args.port, not args.no_window))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception("管理器异常退出")
        if not sys.stderr:
            _show_fatal(f"中转站管理器出错了：{e}\n\n详细信息见 {log_path}")
        raise


if __name__ == "__main__":
    main()
