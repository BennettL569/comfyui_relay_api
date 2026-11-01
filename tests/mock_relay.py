"""本地模拟中转站，覆盖插件支持的各种接口格式，供测试使用。

起两个服务：relay（需要 Key 的接口）和 cdn（模拟第三方存储，记录有没有收到 Key）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json

import av
import numpy as np
from aiohttp import web
from PIL import Image

KEY = "sk-mock0123456789abcdefghijklmnopqrstuv"  # 和真实令牌一样长，粘贴识别才能认出来
KEY_LIMITED = "sk-limited123456789abcdefghijklmnopqrs"  # 限定了模型的令牌：和真实 new-api 一样查不了视频任务


def make_png(color=(200, 30, 30), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_mp4() -> bytes:
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=8)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for i in range(8):
            frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 30, np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return buf.getvalue()


PNG = make_png()
THOUGHT_PNG = make_png((0, 0, 255))
MP4 = make_mp4()
PNG_B64 = base64.b64encode(PNG).decode()
PNG_DATA_URL = "data:image/png;base64," + PNG_B64


class MockRelay:
    def __init__(self, relay_port: int, cdn_port: int):
        self.relay_port, self.cdn_port = relay_port, cdn_port
        self.cdn = f"http://127.0.0.1:{cdn_port}"
        self.requests: list[tuple[str, object]] = []
        self.cdn_auth: list[str | None] = []  # 每次 CDN 请求带的 Authorization
        self.cdn_goog: list[str | None] = []  # 每次 CDN 请求带的 x-goog-api-key
        self.api_auth: list[str | None] = []  # 请求 /api/status、/api/pricing 时带的 Authorization（应该不带）
        self.polls: dict[str, int] = {}
        self._runners: list[web.AppRunner] = []

    def log(self, name: str, info: object) -> None:
        self.requests.append((name, info))

    def last(self, name: str):
        for n, info in reversed(self.requests):
            if n == name:
                return info
        return None

    @web.middleware
    async def auth(self, request: web.Request, handler):
        if request.path.startswith("/api/"):  # 站点信息和价格表是公开的
            return await handler(request)
        token = request.headers.get("Authorization")
        if (request.method == "GET" and token == f"Bearer {KEY_LIMITED}"
                and request.path.startswith(("/v1/video/generations/", "/v1/videos/"))):
            return web.json_response({"error": {"code": "", "message": "该令牌无权访问模型 (request id: mock)",
                                                "type": "new_api_error"}}, status=403)
        ok = token in (f"Bearer {KEY}", f"Bearer {KEY_LIMITED}") or request.headers.get("x-goog-api-key") == KEY
        if not ok:
            return web.json_response({"error": {"message": "无效的令牌", "type": "auth"}}, status=401)
        return await handler(request)

    # ---- 对话 ----
    async def chat(self, request: web.Request):
        body = await request.json()
        self.log("chat", body)
        if body["model"].endswith("-slow"):  # 模拟真实接口的耗时
            await asyncio.sleep(2)
        msgs = body["messages"]
        user = msgs[-1]["content"]
        if isinstance(user, list):
            text = "".join(p.get("text", "") for p in user if p.get("type") == "text")
            n_img = sum(p.get("type") == "image_url" for p in user)
        else:
            text, n_img = user, 0
        system = msgs[0]["content"] if msgs[0]["role"] == "system" else ""
        message = {"role": "assistant", "content": f"echo:{text}|images:{n_img}|system:{system}"}
        model = body["model"]
        if model == "chat-image-md":
            message["content"] = f"好的，这是生成的图片：\n![image]({PNG_DATA_URL})"
        elif model == "chat-image-field":
            message = {"role": "assistant", "content": "完成",
                       "images": [{"type": "image_url", "image_url": {"url": PNG_DATA_URL}}]}
        elif model == "chat-image-link":
            message["content"] = f"![result]({self.cdn}/files/test.png?sig=abc)"
        elif model == "chat-no-image":
            message["content"] = "抱歉，这个请求我没法画。"
        elif model == "chat-video":
            message["content"] = f"排队中…\n生成进度 60%…\n完成：[点击下载]({self.cdn}/files/test.mp4)。"
        elif model == "chat-error-200":
            return web.json_response({"error": {"message": "余额不足"}})
        if body.get("stream"):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            content = message["content"]
            for i in range(0, len(content), 5):
                chunk = {"choices": [{"delta": {"content": content[i:i + 5]}}]}
                await resp.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            return resp
        return web.json_response({"choices": [{"message": message}]})

    # 模型名 -> new-api 标注的接口类型（/v1/models 里带，/api/pricing 里也有）。
    # 和真实 new-api 一致：可灵这类视频任务渠道只会被默认标成 openai。
    MODEL_TYPES = {
        "gpt-4o-mini": ["openai"],
        "mock-chat": ["openai"],
        "gpt-image-1": ["image-generation", "openai"],
        "gemini-3-pro-image-preview": ["gemini", "openai"],
        "doubao-seedream-4-0": ["image-generation", "openai"],
        "sora-2": ["openai-video"],
        "kling-v2-1": ["openai"],
        "MiniMax-H3": ["openai"],
        "wan 3.0-720p-30s": ["openai"],
        "text-embedding-3-small": ["embeddings"],
    }
    TASK_MODELS = {"MiniMax-H3", "wan 3.0-720p-30s"}  # 任务插件认领的模型，和真实 new-api 一样 owned_by 是 task plugin

    async def models(self, request: web.Request):
        data = [{"id": i, "object": "model", "owned_by": "task plugin" if i in self.TASK_MODELS else "custom",
                 "supported_endpoint_types": t} for i, t in self.MODEL_TYPES.items()]
        return web.json_response({"object": "list", "data": data, "success": True})

    async def api_status(self, request: web.Request):
        self.api_auth.append(request.headers.get("Authorization"))
        return web.json_response({"success": True, "message": "", "data": {
            "system_name": "某某中转", "version": "v0.9.9", "setup": True, "HeaderNavModules": "", "quota_display_type": "USD"}})

    async def api_pricing(self, request: web.Request):
        self.api_auth.append(request.headers.get("Authorization"))
        data = [{"model_name": i, "quota_type": 0, "supported_endpoint_types": t} for i, t in self.MODEL_TYPES.items()]
        return web.json_response({"success": True, "data": data})

    # ---- 生图 ----
    async def images_generations(self, request: web.Request):
        body = await request.json()
        self.log("images_generations", body)
        if body["model"] == "img-url":
            return web.json_response({"data": [{"url": f"{self.cdn}/files/test.png"}]})
        n = body.get("n", 1)
        return web.json_response({"data": [{"b64_json": PNG_B64, "revised_prompt": "rp:" + body["prompt"]}] * n})

    async def images_edits(self, request: web.Request):
        form = await request.post()
        self.log("images_edits", {
            "fields": {k: v for k, v in form.items() if isinstance(v, str)},
            "files": [(k, v.filename, v.content_type) for k, v in form.items() if not isinstance(v, str)],
        })
        return web.json_response({"data": [{"b64_json": PNG_B64}]})

    async def gemini(self, request: web.Request):
        body = await request.json()
        tail = request.match_info["tail"]
        self.log("gemini", {"path": tail, "body": body, "goog_key": request.headers.get("x-goog-api-key")})
        if tail.startswith("gemini-blocked"):
            return web.json_response({"promptFeedback": {"blockReason": "SAFETY"}})
        thought = base64.b64encode(THOUGHT_PNG).decode()
        return web.json_response({"candidates": [{"content": {"parts": [
            {"text": "先构思一下", "thought": True},
            {"inlineData": {"mimeType": "image/png", "data": thought}, "thought": True},
            {"text": "这是最终结果"},
            {"inlineData": {"mimeType": "image/png", "data": PNG_B64}},
        ]}, "finishReason": "STOP"}]})

    # ---- 视频：new-api 统一格式 ----
    async def unified_create(self, request: web.Request):
        body = await request.json()
        self.log("unified_create", body)
        tid = "fail-1" if body["model"] == "video-fail" else "u-1"
        self.polls[tid] = 0
        return web.json_response({"task_id": tid, "status": "queued"})

    async def unified_get(self, request: web.Request):
        tid = request.match_info["tid"]
        if tid not in self.polls:
            return web.json_response({"error": {"message": "Task not found"}}, status=404)
        self.polls[tid] += 1
        if self.polls[tid] < 3:
            return web.json_response({"task_id": tid, "status": "in_progress", "progress": self.polls[tid] * 30})
        if tid.startswith("fail"):
            return web.json_response({"task_id": tid, "status": "failed",
                                      "error": {"code": "audit", "message": "内容审核未通过"}})
        return web.json_response({"task_id": tid, "status": "completed", "url": f"{self.cdn}/files/test.mp4",
                                  "format": "mp4", "error": {"code": 0, "message": ""}})

    # ---- 视频：OpenAI / Sora 格式 ----
    async def openai_create(self, request: web.Request):
        if request.content_type.startswith("multipart/"):
            form = await request.post()
            info = {"type": "multipart", "fields": {k: v for k, v in form.items() if isinstance(v, str)},
                    "files": [(k, v.content_type, Image.open(io.BytesIO(v.file.read())).size)
                              for k, v in form.items() if not isinstance(v, str)]}
        else:
            info = {"type": "json", "fields": await request.json()}
            model = info["fields"].get("model", "")
            if model in self.TASK_MODELS or model.startswith("task-"):  # 站点按模型找插件，不用填插件键
                self.log("task_create", {"plugin": "", "body": info["fields"]})
                return self.task_submit(info["fields"], video_object=True)
        self.log("openai_create", info)
        self.polls["video_1"] = 0
        return web.json_response({"id": "video_1", "object": "video", "status": "queued", "progress": 0})

    async def openai_get(self, request: web.Request):
        vid = request.match_info["vid"]
        self.polls[vid] += 1
        status = "in_progress" if self.polls[vid] < 2 else "completed"
        return web.json_response({"id": vid, "object": "video", "status": status, "progress": 50 * self.polls[vid]})

    async def openai_content(self, request: web.Request):
        self.log("openai_content", {"auth": request.headers.get("Authorization")})
        return web.Response(body=MP4, content_type="video/mp4")

    # ---- 视频：可灵原生格式（测自定义路径）----
    async def kling_create(self, request: web.Request):
        body = await request.json()
        self.log("kling_create", body)
        self.polls["k-1"] = 0
        return web.json_response({"code": 0, "message": "SUCCEED", "request_id": "req-9",
                                  "data": {"task_id": "k-1", "task_status": "submitted"}})

    async def kling_get(self, request: web.Request):
        tid = request.match_info["tid"]
        self.polls[tid] += 1
        if self.polls[tid] < 2:
            return web.json_response({"code": 0, "message": "SUCCEED", "data": {"task_id": tid, "task_status": "processing"}})
        return web.json_response({"code": 0, "message": "SUCCEED", "data": {
            "task_id": tid, "task_status": "succeed",
            "task_result": {"videos": [{"id": "v1", "url": f"{self.cdn}/files/test.mp4", "duration": "5"}]},
        }})

    # ---- 视频：new-api 任务插件（返回内容按官方文档）----
    # 和真实站点一样：模型绑在站长自己的 custom-h3 上。官方的 hailuo 也装了，但模型不归它，站点只回「无可用渠道」
    def task_no_channel(self, model: str) -> web.Response:
        return web.json_response({"error": {"code": "model_not_found", "type": "new_api_error", "message":
                                            f"分组 默认 下模型 {model} 无可用渠道（distributor）"}}, status=503)

    def task_submit(self, body: dict, *, video_object: bool) -> web.Response:
        model = body.get("model")
        if model == "task-no-channel":
            return self.task_no_channel(model)
        if model == "task-invalid":  # 已经到了插件这一步：换入口也一样，不能重复提交
            return web.json_response({"code": "plugin_request_invalid", "data": None, "message":
                                      "plugin custom-h3@1.3.1 hook buildSubmitRequest failed: requires a prompt"}, status=400)
        tid = {"task-fail": "task-fail-1", "task-flaky": "task-flaky-1"}.get(model, "task-1")
        self.polls[tid] = 0
        extra = {"object": "video", "progress": 0} if video_object else {"task_id": tid}
        return web.json_response({"id": tid, "status": "queued", "model": model, "created_at": 1780000000, **extra})

    async def task_create(self, request: web.Request):
        plugin = request.match_info["plugin"]
        body = await request.json()
        self.log("task_create", {"plugin": plugin, "body": body})
        if plugin not in ("hailuo", "custom-h3"):
            return web.json_response({"error": {"message": "task plugin not found",
                                                "type": "invalid_request_error"}}, status=400)
        if plugin != "custom-h3":
            return self.task_no_channel(body.get("model"))
        return self.task_submit(body, video_object=False)

    async def task_get(self, request: web.Request):
        tid = request.match_info["tid"]
        if tid not in self.polls:
            return web.json_response({"error": {"message": "Task not found"}}, status=404)
        self.polls[tid] += 1
        if tid == "task-flaky-1" and self.polls[tid] == 1:
            return web.Response(status=502, text="<html><title>502 Bad Gateway</title></html>", content_type="text/html")
        common = {"task_id": tid, "platform": "hailuo", "created_at": 1780000000, "fail_reason": "", "finished_at": 0}
        if self.polls[tid] == 1:
            return web.json_response({**common, "status": "QUEUED", "progress": "30%"})
        if self.polls[tid] == 2:
            return web.json_response({**common, "status": "IN_PROGRESS", "progress": "50%"})
        if tid == "task-fail-1":
            return web.json_response({**common, "status": "FAILURE", "progress": "100%", "fail_reason": "内容审核未通过"})
        return web.json_response({**common, "status": "SUCCESS", "progress": "100%"})  # 查询结果里没有视频地址

    async def task_artifacts(self, request: web.Request):
        secret = f"http://127.0.0.1:{self.relay_port}/signed?credential=secret"
        return web.json_response({"artifacts": [
            {"key": "cover", "type": "image", "mime_type": "image/png", "content_url": secret},
            {"key": "clip", "type": "video", "mime_type": "video/mp4", "content_url": secret},
        ]})

    async def task_content(self, request: web.Request):
        self.log("task_content", {"key": request.match_info["key"], "auth": request.headers.get("Authorization")})
        return web.Response(body=MP4, content_type="video/mp4")

    # ---- 中转站自己的文件和重定向 ----
    async def owned_file(self, request: web.Request):
        return web.Response(body=PNG, content_type="image/png")

    async def redirect_to(self, request: web.Request):
        return web.Response(status=302, headers={"Location": request.query["to"]})

    # ---- 第三方存储 ----
    async def cdn_file(self, request: web.Request):
        self.cdn_auth.append(request.headers.get("Authorization"))
        self.cdn_goog.append(request.headers.get("x-goog-api-key"))
        name = request.match_info["name"]
        if name == "test.png":
            return web.Response(body=PNG, content_type="image/png")
        if name == "test.mp4":
            return web.Response(body=MP4, content_type="video/mp4")
        return web.json_response({"error": {"message": "internal-secret"}}, status=404)

    async def start(self) -> None:
        relay = web.Application(middlewares=[self.auth], client_max_size=64 * 1024 * 1024)
        relay.add_routes([
            web.get("/api/status", self.api_status),
            web.get("/api/pricing", self.api_pricing),
            web.get("/v1/models", self.models),
            web.get("/owned/{name}", self.owned_file),
            web.get("/redirect", self.redirect_to),
            web.post("/redirect", self.redirect_to),
            web.post("/v1/chat/completions", self.chat),
            web.post("/v1/images/generations", self.images_generations),
            web.post("/v1/images/edits", self.images_edits),
            web.post("/v1beta/models/{tail:.+}", self.gemini),
            web.post("/v1/video/generations", self.unified_create),
            web.get("/v1/video/generations/{tid}", self.unified_get),
            web.post("/v1/videos", self.openai_create),
            web.get("/v1/videos/{vid}", self.openai_get),
            web.get("/v1/videos/{vid}/content", self.openai_content),
            web.post("/kling/v1/videos/image2video", self.kling_create),
            web.get("/kling/v1/videos/image2video/{tid}", self.kling_get),
            web.post("/v1/tasks/{plugin}", self.task_create),
            web.get("/v1/tasks/{tid}", self.task_get),
            web.get("/v1/tasks/{tid}/artifacts", self.task_artifacts),
            web.get("/v1/tasks/{tid}/artifacts/{key}/content", self.task_content),
        ])
        cdn = web.Application()
        cdn.add_routes([web.get("/files/{name}", self.cdn_file)])
        for app, port in ((relay, self.relay_port), (cdn, self.cdn_port)):
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            self._runners.append(runner)

    async def stop(self) -> None:
        for runner in self._runners:
            await runner.cleanup()


if __name__ == "__main__":
    import sys

    async def serve() -> None:
        mock = MockRelay(int(sys.argv[1]), int(sys.argv[2]))
        await mock.start()
        print(f"模拟中转站已启动：http://127.0.0.1:{mock.relay_port}/v1  Key：{KEY}", flush=True)
        await asyncio.Event().wait()

    asyncio.run(serve())
