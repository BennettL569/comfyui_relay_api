"""ComfyUI 中转站 API 节点：文本对话 / 生图 / 生视频，支持同时配置多个中转站，自带中转站管理器。"""
from comfy_api.latest import ComfyExtension, io
from server import PromptServer

from . import manager_api
from .nodes import RelayChat, RelayImage, RelayModels, RelayVideo

WEB_DIRECTORY = "./web"


def _register_routes() -> None:
    """管理器页面和接口：/relay_api/...（左侧栏「中转站」面板和桌面上的独立管理器都会用到）。"""
    server = getattr(PromptServer, "instance", None)
    if server is None:
        return

    def send(event: dict) -> int:
        clients = list(server.sockets)
        if not clients:
            return 0
        if event["type"] == "add-node":
            # 只发给最近连上的那个页面，免得开着几个 ComfyUI 页面时每个都加一个节点
            server.send_sync("relay_api.event", event, clients[-1])
            return 1
        server.send_sync("relay_api.event", event)
        return len(clients)

    manager_api.add_routes(server.routes)
    manager_api.add_comfy_routes(server.routes, send)


_register_routes()


class RelayAPIExtension(ComfyExtension):
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [RelayChat, RelayImage, RelayVideo, RelayModels]


async def comfy_entrypoint() -> RelayAPIExtension:
    return RelayAPIExtension()
