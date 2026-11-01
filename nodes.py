"""ComfyUI 节点：文本对话、生图、生视频、模型列表。请求细节都在 relay_client.py。"""
from __future__ import annotations

import base64
import logging
import re
import time
from io import BytesIO

import numpy as np
import torch
from PIL import Image, ImageOps

import comfy.model_management
import comfy.utils
from comfy_api.latest import InputImpl, io, ui
from server import PromptServer

from . import relay_client as rc

log = logging.getLogger("comfyui_relay_api")

CATEGORY = "中转站API"

IMAGE_FORMAT_TIP = (
    "images：OpenAI 图像接口（gpt-image、dall-e、flux 等），有参考图时走 /v1/images/edits\n"
    "images-json：参考图放进 image 字段发到 /v1/images/generations（Seedream / 即梦等）\n"
    "chat：走对话接口出图（多数站的 nano-banana 用这个）\n"
    "gemini：Gemini 原生 /v1beta 接口（nano-banana，可设宽高比和 4K）"
)
VIDEO_FORMAT_TIP = (
    "unified：new-api 统一视频接口 /v1/video/generations（可灵、海螺、Vidu、Seedance 等多数站都支持）\n"
    "openai：OpenAI / Sora 格式 /v1/videos\n"
    "chat：走对话接口出视频，回复里带视频链接（部分站的 veo、sora）\n"
    "custom：自己填提交和查询路径，适配厂商原生格式\n"
    "task：new-api 任务插件模型（模型列表里标着 task plugin，如 MiniMax-H3），站点按模型自动找插件"
)
HAILUO_WORDS = ("minimax-h", "hailuo", "t2v-01", "i2v-01", "s2v-01")


# ---------- 公共输入 ----------

def relay_input() -> io.Combo.Input:
    try:
        # 没填过的示例占位站不放进下拉框，免得新节点默认选中一个用不了的站
        names = [name for name, cfg in rc.load_relays().items()
                 if not (rc._unfilled(str(cfg.get("base_url") or "").strip())
                         and rc._unfilled(str(cfg.get("api_key") or "").strip()) and not cfg.get("api_key_env"))]
    except Exception as e:  # 配置写坏了也不能让节点加载失败
        log.error("[中转站] 读取配置失败：%s", e)
        names = []
    return io.Combo.Input(
        "relay", options=names or ["（先在左侧「中转站」面板里添加中转站）"], display_name="中转站",
        tooltip="在左侧「中转站」面板或独立的「中转站管理器」里添加。手动改了 relays.json 的话按 R 刷新节点即可。",
    )


def model_input(placeholder: str) -> io.String.Input:
    return io.String.Input("model", display_name="模型", default="", placeholder=placeholder,
                           tooltip="中转站里的模型名，可以用「中转站 · 模型列表」节点查询")


def prompt_input() -> io.String.Input:
    return io.String.Input("prompt", display_name="提示词", multiline=True, default="")


def seed_input() -> io.Int.Input:
    return io.Int.Input("seed", default=0, min=0, max=0xFFFFFFFF, control_after_generate=True,
                        tooltip="不会发给接口，只决定要不要重新请求：值变了才重新调用（会扣费），固定不变就复用上次结果")


def extra_input() -> io.String.Input:
    return io.String.Input("extra_json", display_name="额外参数", multiline=True, default="", advanced=True,
                           tooltip='JSON 对象，会合并进请求体，如 {"quality": "high"}；值写 null 表示删掉该字段')


def timeout_input(default: int) -> io.Int.Input:
    return io.Int.Input("timeout", display_name="超时(秒)", default=default, min=10, max=7200, advanced=True)


def _require(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise rc.RelayError(f"请填写「{label}」")
    return value


def _check_interrupt() -> None:
    comfy.model_management.throw_exception_if_processing_interrupted()


def _progress_reporter():
    bar = comfy.utils.ProgressBar(100)
    return lambda percent: bar.update_absolute(int(percent), 100)


def _show(cls: type[io.ComfyNode], text: str) -> None:
    """运行期间在节点上显示状态文字（官方 API 节点显示进度用的也是这个，运行结束后前端会清掉）。"""
    node_id = cls.hidden.unique_id if cls.hidden else None
    if node_id and PromptServer.instance is not None:
        PromptServer.instance.send_progress_text(text, node_id)


# ---------- 图片 / 视频转换 ----------

def _to_pil(frame: torch.Tensor) -> Image.Image:
    arr = (frame.detach().cpu().float().clamp(0, 1).numpy() * 255).round().astype(np.uint8)
    return Image.fromarray(arr[..., 0] if arr.shape[-1] == 1 else arr)


def _encode(img: Image.Image) -> tuple[bytes, str]:
    """优先 PNG；超过 4MB 改成高质量 JPEG，避免请求体超限。"""
    buf = BytesIO()
    img.save(buf, format="PNG")
    if buf.tell() <= 4 * 1024 * 1024:
        return buf.getvalue(), "image/png"
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue(), "image/jpeg"


def _batch_refs(images: torch.Tensor | None) -> list[tuple[bytes, str]]:
    return [] if images is None else [_encode(_to_pil(frame)) for frame in images]


def _first_frame(images: torch.Tensor | None) -> Image.Image | None:
    return None if images is None else _to_pil(images[0])


def _encode_field(img: Image.Image, encoding: str) -> str:
    blob, mime = _encode(img)
    return rc.to_data_url(blob, mime) if encoding == "data_url" else base64.b64encode(blob).decode()


def _to_tensor(blobs: list[bytes]) -> torch.Tensor:
    pics = []
    for blob in blobs:
        try:
            pics.append(ImageOps.exif_transpose(Image.open(BytesIO(blob))).convert("RGB"))
        except Exception as e:
            raise rc.RelayError(f"返回的图片无法解码：{e}") from None
    # 同一批次尺寸必须一致，不一致的缩放到第一张的尺寸
    size = pics[0].size
    arrays = [
        np.asarray(p if p.size == size else p.resize(size, Image.Resampling.LANCZOS), dtype=np.float32) / 255.0
        for p in pics
    ]
    return torch.from_numpy(np.stack(arrays))


def _to_video(blob: bytes) -> InputImpl.VideoFromFile:
    video = InputImpl.VideoFromFile(BytesIO(blob))
    try:
        video.get_dimensions()
    except Exception as e:
        raise rc.RelayError(f"下载到的文件不是有效视频（{len(blob)} 字节）：{e}") from None
    return video


def _parse_size(text: str) -> tuple[int, int] | None:
    text = (text or "").strip().lower()
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*[x*×]\s*(\d+)", text)
    if not m:
        raise rc.RelayError(f"分辨率格式应为 宽x高，例如 1280x720，现在是「{text}」")
    return int(m.group(1)), int(m.group(2))


# ---------- 节点 ----------

class RelayChat(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RelayAPI_Chat",
            display_name="中转站 · 文本对话",
            category=CATEGORY,
            description="调用中转站的 /v1/chat/completions（OpenAI 兼容格式）。接上图片就能识图、反推提示词。",
            search_aliases=["relay", "中转站", "llm", "chat", "gpt", "deepseek", "claude", "gemini", "反推", "扩写"],
            inputs=[
                relay_input(),
                model_input("如 gpt-4o-mini / deepseek-chat / claude-sonnet-4-5"),
                prompt_input(),
                io.String.Input("system", display_name="系统提示词", multiline=True, default=""),
                io.Image.Input("images", display_name="图片", optional=True,
                               tooltip="可选，接上后图片会一起发给模型（模型需支持识图）"),
                io.Float.Input("temperature", display_name="温度", default=-1.0, min=-1.0, max=2.0, step=0.05,
                               tooltip="-1 表示不发送，用模型默认值（部分推理模型只接受默认值）"),
                io.Int.Input("max_tokens", display_name="最大输出", default=0, min=0, max=1_000_000,
                             tooltip="0 表示不发送"),
                seed_input(),
                extra_input(),
                timeout_input(300),
            ],
            outputs=[io.String.Output(display_name="文本")],
            hidden=[io.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    async def execute(cls, relay, model, prompt, system, temperature, max_tokens, seed, extra_json, timeout,
                      images=None) -> io.NodeOutput:
        r = rc.get_relay(relay)
        _show(cls, "请求中…")
        messages = [{"role": "system", "content": system}] if system.strip() else []
        messages.append({"role": "user", "content": rc.chat_content(prompt, _batch_refs(images))})
        body = {"model": _require(model, "模型"), "messages": messages}
        if temperature >= 0:
            body["temperature"] = temperature
        if max_tokens > 0:
            body["max_tokens"] = max_tokens
        body = rc.merge_extra(body, rc.parse_extra(extra_json))
        text, _ = await rc.chat_completion(
            r, body, timeout=timeout, check_interrupt=_check_interrupt, stream=bool(body.get("stream"))
        )
        return io.NodeOutput(text, ui=ui.PreviewText(text))


class RelayImage(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RelayAPI_Image",
            display_name="中转站 · 生图",
            category=CATEGORY,
            description="通过中转站生图或改图，支持 OpenAI 图像接口、Seedream 类接口、对话接口出图和 Gemini 原生接口。",
            search_aliases=["relay", "中转站", "image", "生图", "改图", "gpt-image", "nano banana", "gemini",
                            "seedream", "flux"],
            inputs=[
                relay_input(),
                model_input("如 gpt-image-1 / gemini-3-pro-image-preview / doubao-seedream-4-0"),
                prompt_input(),
                io.Image.Input("images", display_name="参考图", optional=True,
                               tooltip="可选，一个批次里的多张图会作为多张参考图一起发送"),
                io.DynamicCombo.Input("api_format", display_name="接口格式", tooltip=IMAGE_FORMAT_TIP, options=[
                    io.DynamicCombo.Option("images", [
                        io.String.Input("size", display_name="尺寸", default="",
                                        placeholder="如 1024x1024 / 1536x1024 / auto，留空用默认"),
                        io.Int.Input("n", display_name="张数", default=1, min=1, max=10),
                        io.String.Input("quality", display_name="质量", default="",
                                        placeholder="如 high / medium / low，留空用默认"),
                    ]),
                    io.DynamicCombo.Option("images-json", [
                        io.String.Input("size", display_name="尺寸", default="",
                                        placeholder="如 2K / 4K / 2048x2048，留空用默认"),
                    ]),
                    io.DynamicCombo.Option("chat", []),
                    io.DynamicCombo.Option("gemini", [
                        io.Combo.Input("aspect_ratio", display_name="宽高比", default="auto", options=[
                            "auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]),
                        io.Combo.Input("image_size", display_name="分辨率", default="auto",
                                       options=["auto", "1K", "2K", "4K"]),
                    ]),
                ]),
                seed_input(),
                extra_input(),
                timeout_input(300),
            ],
            outputs=[io.Image.Output(display_name="图片"), io.String.Output(display_name="文本")],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    async def execute(cls, relay, model, prompt, api_format, seed, extra_json, timeout, images=None) -> io.NodeOutput:
        r = rc.get_relay(relay)
        model = _require(model, "模型")
        _show(cls, "生成中…")
        refs = _batch_refs(images)
        opts = dict(extra=rc.parse_extra(extra_json), timeout=timeout, check_interrupt=_check_interrupt)
        fmt = api_format["api_format"]
        if fmt in ("images", "images-json"):
            blobs, text = await rc.images_api(
                r, model, prompt, refs,
                size=(api_format.get("size") or "").strip(),
                n=api_format.get("n") or 1,
                quality=(api_format.get("quality") or "").strip(),
                ref_mode="edits" if fmt == "images" else "json",
                **opts,
            )
        elif fmt == "chat":
            blobs, text = await rc.chat_image(r, model, prompt, refs, **opts)
        else:
            blobs, text = await rc.gemini_image(
                r, model, prompt, refs,
                aspect_ratio=api_format.get("aspect_ratio") or "auto",
                image_size=api_format.get("image_size") or "auto",
                **opts,
            )
        return io.NodeOutput(_to_tensor(blobs), text)


class RelayVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RelayAPI_Video",
            display_name="中转站 · 生视频",
            category=CATEGORY,
            description="通过中转站生成视频：提交任务、轮询进度、下载成片。接上图片就是图生视频。",
            search_aliases=["relay", "中转站", "video", "生视频", "sora", "veo", "kling", "可灵", "海螺",
                            "hailuo", "minimax", "vidu", "seedance", "即梦"],
            inputs=[
                relay_input(),
                model_input("如 sora-2 / veo3.1 / kling-v2-1 / MiniMax-H3"),
                prompt_input(),
                io.Image.Input("image", display_name="首帧图", optional=True, tooltip="可选，接上后为图生视频（只用第一张）"),
                io.DynamicCombo.Input("api_format", display_name="接口格式", tooltip=VIDEO_FORMAT_TIP, options=[
                    io.DynamicCombo.Option("unified", [
                        io.Int.Input("duration", display_name="时长(秒)", default=5, min=0, max=120,
                                     tooltip="0 表示不发送"),
                        io.String.Input("size", display_name="分辨率", default="",
                                        placeholder="如 1280x720，留空用默认"),
                        io.Combo.Input("image_encoding", display_name="图片编码", default="data_url",
                                       options=["data_url", "base64"],
                                       tooltip="图生视频时 image 字段的写法，报错时换另一个试试"),
                    ]),
                    io.DynamicCombo.Option("openai", [
                        io.Int.Input("seconds", display_name="时长(秒)", default=0, min=0, max=120,
                                     tooltip="0 表示不发送。Sora 2 支持 4 / 8 / 12"),
                        io.String.Input("size", display_name="分辨率", default="",
                                        placeholder="如 1280x720 / 720x1280，留空用默认"),
                        io.Combo.Input("body_format", display_name="请求格式", default="multipart",
                                       options=["multipart", "json"]),
                    ]),
                    io.DynamicCombo.Option("chat", [
                        io.Boolean.Input("stream", display_name="流式", default=True,
                                         tooltip="视频生成耗时长，多数站要求流式"),
                    ]),
                    io.DynamicCombo.Option("custom", [
                        io.String.Input("submit_path", display_name="提交路径", default="",
                                        placeholder="如 /kling/v1/videos/image2video"),
                        io.String.Input("query_path", display_name="查询路径", default="",
                                        placeholder="如 /kling/v1/videos/image2video/{task_id}"),
                        io.String.Input("image_field", display_name="图片字段", default="image",
                                        tooltip="首帧图放进请求体的哪个字段，留空则不发送"),
                        io.Combo.Input("image_encoding", display_name="图片编码", default="data_url",
                                       options=["data_url", "base64"]),
                    ]),
                    io.DynamicCombo.Option("task", [
                        io.String.Input("plugin_key", display_name="任务插件", default="",
                                        placeholder="一般留空，站点按模型自动找",
                                        tooltip="留空时提交到 /v1/videos，站点按模型找插件。"
                                                "站长让你指定时才填，会提交到 /v1/tasks/{插件}"),
                        io.Int.Input("duration", display_name="时长(秒)", default=5, min=0, max=120,
                                     tooltip="0 表示不发送。MiniMax-H3 支持 4 到 15"),
                        io.String.Input("size", display_name="分辨率", default="",
                                        placeholder="如 768P / 2K / 1280x720，留空用默认"),
                        io.Combo.Input("ratio", display_name="宽高比", default="auto",
                                       options=["auto", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"],
                                       tooltip="auto 表示不发送。adaptive 是跟随首帧图，只有接了图才能用"),
                    ]),
                ]),
                seed_input(),
                io.Int.Input("poll_interval", display_name="轮询间隔(秒)", default=5, min=1, max=120, advanced=True),
                timeout_input(1200),
                extra_input(),
            ],
            outputs=[io.Video.Output(display_name="视频"), io.String.Output(display_name="视频地址")],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    async def execute(cls, relay, model, prompt, api_format, seed, poll_interval, timeout, extra_json,
                      image=None) -> io.NodeOutput:
        r = rc.get_relay(relay)
        model = _require(model, "模型")
        extra = rc.parse_extra(extra_json)
        first = _first_frame(image)
        fmt = api_format["api_format"]
        _show(cls, "提交任务中…")
        wait = dict(poll_interval=poll_interval, timeout=timeout, check_interrupt=_check_interrupt,
                    on_progress=_progress_reporter(), on_status=lambda text: _show(cls, text))

        if fmt == "unified":
            body = {"model": model, "prompt": prompt}
            if api_format.get("duration"):
                body["duration"] = api_format["duration"]
            size = _parse_size(api_format.get("size"))
            if size:
                body["width"], body["height"] = size
            if first:
                body["image"] = _encode_field(first, api_format.get("image_encoding") or "data_url")
            video, url = await rc.video_unified(r, rc.merge_extra(body, extra), **wait)

        elif fmt == "openai":
            fields = {"model": model, "prompt": prompt}
            if api_format.get("seconds"):
                fields["seconds"] = str(api_format["seconds"])
            size_text = (api_format.get("size") or "").strip()
            if size_text:
                fields["size"] = size_text
            reference = None
            if first:
                size = _parse_size(size_text)
                if size and first.size != size:  # Sora 要求参考图和视频尺寸一致
                    first = ImageOps.fit(first, size, Image.Resampling.LANCZOS)
                reference = _encode(first)
            video, url = await rc.video_openai(
                r, rc.merge_extra(fields, extra), reference,
                multipart=api_format.get("body_format") != "json", **wait,
            )

        elif fmt == "chat":
            content = rc.chat_content(prompt, [_encode(first)] if first else [])
            body = rc.merge_extra({"model": model, "messages": [{"role": "user", "content": content}]}, extra)
            _show(cls, "生成中（对话接口出视频一般要几分钟）…")
            video, url = await rc.video_chat(
                r, body, stream=bool(api_format.get("stream", True)), timeout=timeout, check_interrupt=_check_interrupt
            )

        elif fmt == "task":
            plugin = (api_format.get("plugin_key") or "").strip()
            body = {"model": model, "prompt": prompt}
            if api_format.get("duration"):
                body["duration"] = api_format["duration"]
            size_text = (api_format.get("size") or "").strip()
            if size_text:
                body["size"] = size_text
            metadata = {}
            if (api_format.get("ratio") or "auto") != "auto":
                metadata["ratio"] = api_format["ratio"]
            if first:
                image = _encode_field(first, "data_url")
                if any(w in model.lower() for w in HAILUO_WORDS):
                    metadata["first_frame_image"] = image  # 海螺类插件只有这个字段对它的所有模型都有效
                else:
                    body["images"] = [image]
            if metadata:
                body["metadata"] = metadata
            video, url = await rc.video_task(r, plugin, rc.merge_extra(body, extra), **wait)

        else:
            body = {"model": model, "prompt": prompt}
            field = (api_format.get("image_field") or "").strip()
            if first and field:
                body[field] = _encode_field(first, api_format.get("image_encoding") or "data_url")
            video, url = await rc.video_custom(
                r, _require(api_format.get("submit_path"), "提交路径"),
                (api_format.get("query_path") or "").strip(), rc.merge_extra(body, extra), **wait,
            )

        return io.NodeOutput(_to_video(video), url)


class RelayModels(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RelayAPI_Models",
            display_name="中转站 · 模型列表",
            category=CATEGORY,
            description="列出中转站 /v1/models 里的模型名，方便填进其他节点。",
            search_aliases=["relay", "中转站", "models", "模型"],
            inputs=[
                relay_input(),
                io.String.Input("keyword", display_name="关键字", default="",
                                placeholder="可选，如 image / video / gpt，多个用空格隔开"),
            ],
            outputs=[io.String.Output(display_name="模型列表")],
            hidden=[io.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return time.time()  # 每次运行都重新拉取

    @classmethod
    async def execute(cls, relay, keyword) -> io.NodeOutput:
        ids = await rc.list_models(rc.get_relay(relay))
        words = keyword.lower().split()
        if words:
            ids = [i for i in ids if any(w in i.lower() for w in words)]
        text = f"{relay}：{len(ids)} 个模型\n" + ("\n".join(ids) if ids else "（没有匹配的模型）")
        return io.NodeOutput(text, ui=ui.PreviewText(text))
