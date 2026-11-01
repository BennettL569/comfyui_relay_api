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
from .relay_i18n import tr

log = logging.getLogger("comfyui_relay_api")

CATEGORY = "API Relay"

IMAGE_FORMAT_TIP = (
    "images: OpenAI image API (gpt-image, dall-e, flux...). Uses /v1/images/edits when there are reference images\n"
    "images-json: sends reference images in the image field to /v1/images/generations (Seedream, Jimeng...)\n"
    "chat: gets the image through the chat API (nano-banana on most relays)\n"
    "gemini: native Gemini /v1beta API (nano-banana, with aspect ratio and 4K options)"
)
VIDEO_FORMAT_TIP = (
    "unified: new-api's unified video API /v1/video/generations (Kling, Hailuo, Vidu, Seedance... on most relays)\n"
    "openai: OpenAI / Sora format /v1/videos\n"
    "chat: gets the video through the chat API, with a link in the reply (veo and sora on some relays)\n"
    "custom: your own submit and query paths, for vendor-specific formats\n"
    "task: new-api task-plugin models (marked task plugin in the model list, such as MiniMax-H3). The relay picks the plugin"
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
        log.error("[relay] cannot read the config: %s", e)
        names = []
    return io.Combo.Input(
        "relay", options=names or [tr("(add a relay in the Relay Manager first)")], display_name="relay",
        tooltip="Add relays in the Relay Manager window (tools/start_manager in the plugin folder). After editing relays.json by hand, press R to refresh.",
    )


def model_input(placeholder: str) -> io.String.Input:
    return io.String.Input("model", display_name="model", default="", placeholder=placeholder,
                           tooltip="Model name on the relay. The Relay Models node lists them")


def prompt_input() -> io.String.Input:
    return io.String.Input("prompt", display_name="prompt", multiline=True, default="")


def seed_input() -> io.Int.Input:
    return io.Int.Input("seed", default=0, min=0, max=0xFFFFFFFF, control_after_generate=True,
                        tooltip="Not sent to the API. It only decides whether to call again: a new value makes a new (billed) call, the same value reuses the last result")


def extra_input() -> io.String.Input:
    return io.String.Input("extra_json", display_name="extra params", multiline=True, default="", advanced=True,
                           tooltip='JSON object merged into the request body, such as {"quality": "high"}. Use null to remove a field')


def timeout_input(default: int) -> io.Int.Input:
    return io.Int.Input("timeout", display_name="timeout (s)", default=default, min=10, max=7200, advanced=True)


def _require(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise rc.RelayError(tr('Please fill in "{label}"', label=label))
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
            raise rc.RelayError(tr("The returned image cannot be decoded: {error}", error=e)) from None
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
        raise rc.RelayError(tr("The downloaded file is not a valid video ({size} bytes): {error}", size=len(blob), error=e)) from None
    return video


def _parse_size(text: str) -> tuple[int, int] | None:
    text = (text or "").strip().lower()
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*[x*×]\s*(\d+)", text)
    if not m:
        raise rc.RelayError(tr('Size must be WIDTHxHEIGHT, for example 1280x720, not "{text}"', text=text))
    return int(m.group(1)), int(m.group(2))


# ---------- 节点 ----------

class RelayChat(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RelayAPI_Chat",
            display_name="Relay Chat",
            category=CATEGORY,
            description="Calls /v1/chat/completions on a relay (OpenAI-compatible). Connect images for vision models or prompt extraction.",
            search_aliases=["relay", "中转站", "llm", "chat", "gpt", "deepseek", "claude", "gemini", "反推", "扩写"],
            inputs=[
                relay_input(),
                model_input("gpt-4o-mini / deepseek-chat / claude-sonnet-4-5"),
                prompt_input(),
                io.String.Input("system", display_name="system prompt", multiline=True, default=""),
                io.Image.Input("images", display_name="images", optional=True,
                               tooltip="Optional. Images are sent to the model too (it must support vision)"),
                io.Float.Input("temperature", display_name="temperature", default=-1.0, min=-1.0, max=2.0, step=0.05,
                               tooltip="-1 means not sent, using the model's default (some reasoning models only accept that)"),
                io.Int.Input("max_tokens", display_name="max tokens", default=0, min=0, max=1_000_000,
                             tooltip="0 means not sent"),
                seed_input(),
                extra_input(),
                timeout_input(300),
            ],
            outputs=[io.String.Output(display_name="text")],
            hidden=[io.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    async def execute(cls, relay, model, prompt, system, temperature, max_tokens, seed, extra_json, timeout,
                      images=None) -> io.NodeOutput:
        r = rc.get_relay(relay)
        _show(cls, tr("Requesting…"))
        messages = [{"role": "system", "content": system}] if system.strip() else []
        messages.append({"role": "user", "content": rc.chat_content(prompt, _batch_refs(images))})
        body = {"model": _require(model, tr("model")), "messages": messages}
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
            display_name="Relay Image",
            category=CATEGORY,
            description="Generates or edits images through a relay: OpenAI image API, Seedream-style API, images through chat, or native Gemini.",
            search_aliases=["relay", "中转站", "image", "生图", "改图", "gpt-image", "nano banana", "gemini",
                            "seedream", "flux"],
            inputs=[
                relay_input(),
                model_input("gpt-image-1 / gemini-3-pro-image-preview / doubao-seedream-4-0"),
                prompt_input(),
                io.Image.Input("images", display_name="reference images", optional=True,
                               tooltip="Optional. Every image in the batch is sent as a reference image"),
                io.DynamicCombo.Input("api_format", display_name="API format", tooltip=IMAGE_FORMAT_TIP, options=[
                    io.DynamicCombo.Option("images", [
                        io.String.Input("size", display_name="size", default="",
                                        placeholder="1024x1024 / 1536x1024 / auto. Empty for default"),
                        io.Int.Input("n", display_name="count", default=1, min=1, max=10),
                        io.String.Input("quality", display_name="quality", default="",
                                        placeholder="high / medium / low. Empty for default"),
                    ]),
                    io.DynamicCombo.Option("images-json", [
                        io.String.Input("size", display_name="size", default="",
                                        placeholder="2K / 4K / 2048x2048. Empty for default"),
                    ]),
                    io.DynamicCombo.Option("chat", []),
                    io.DynamicCombo.Option("gemini", [
                        io.Combo.Input("aspect_ratio", display_name="aspect ratio", default="auto", options=[
                            "auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]),
                        io.Combo.Input("image_size", display_name="resolution", default="auto",
                                       options=["auto", "1K", "2K", "4K"]),
                    ]),
                ]),
                seed_input(),
                extra_input(),
                timeout_input(300),
            ],
            outputs=[io.Image.Output(display_name="image"), io.String.Output(display_name="text")],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    async def execute(cls, relay, model, prompt, api_format, seed, extra_json, timeout, images=None) -> io.NodeOutput:
        r = rc.get_relay(relay)
        model = _require(model, tr("model"))
        _show(cls, tr("Generating…"))
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
            display_name="Relay Video",
            category=CATEGORY,
            description="Generates video through a relay: submits the task, polls progress and downloads the result. Connect an image for image-to-video.",
            search_aliases=["relay", "中转站", "video", "生视频", "sora", "veo", "kling", "可灵", "海螺",
                            "hailuo", "minimax", "vidu", "seedance", "即梦"],
            inputs=[
                relay_input(),
                model_input("sora-2 / veo3.1 / kling-v2-1 / MiniMax-H3"),
                prompt_input(),
                io.Image.Input("image", display_name="first frame", optional=True, tooltip="Optional. Makes this image-to-video (only the first image is used)"),
                io.DynamicCombo.Input("api_format", display_name="API format", tooltip=VIDEO_FORMAT_TIP, options=[
                    io.DynamicCombo.Option("unified", [
                        io.Int.Input("duration", display_name="duration (s)", default=5, min=0, max=120,
                                     tooltip="0 means not sent"),
                        io.String.Input("size", display_name="resolution", default="",
                                        placeholder="1280x720. Empty for default"),
                        io.Combo.Input("image_encoding", display_name="image encoding", default="data_url",
                                       options=["data_url", "base64"],
                                       tooltip="How the image field is written for image-to-video. Try the other one if the relay reports an error"),
                    ]),
                    io.DynamicCombo.Option("openai", [
                        io.Int.Input("seconds", display_name="duration (s)", default=0, min=0, max=120,
                                     tooltip="0 means not sent. Sora 2 supports 4 / 8 / 12"),
                        io.String.Input("size", display_name="resolution", default="",
                                        placeholder="1280x720 / 720x1280. Empty for default"),
                        io.Combo.Input("body_format", display_name="request format", default="multipart",
                                       options=["multipart", "json"]),
                    ]),
                    io.DynamicCombo.Option("chat", [
                        io.Boolean.Input("stream", display_name="stream", default=True,
                                         tooltip="Video takes a long time, so most relays require streaming"),
                    ]),
                    io.DynamicCombo.Option("custom", [
                        io.String.Input("submit_path", display_name="submit path", default="",
                                        placeholder="/kling/v1/videos/image2video"),
                        io.String.Input("query_path", display_name="query path", default="",
                                        placeholder="/kling/v1/videos/image2video/{task_id}"),
                        io.String.Input("image_field", display_name="image field", default="image",
                                        tooltip="Request body field for the first frame. Empty means not sent"),
                        io.Combo.Input("image_encoding", display_name="image encoding", default="data_url",
                                       options=["data_url", "base64"]),
                    ]),
                    io.DynamicCombo.Option("task", [
                        io.String.Input("plugin_key", display_name="task plugin", default="",
                                        placeholder="Usually empty: the relay picks by model",
                                        tooltip="Empty submits to /v1/videos and the relay picks the plugin. "
                                                "Fill it in only if the relay tells you to: it submits to /v1/tasks/{plugin}"),
                        io.Int.Input("duration", display_name="duration (s)", default=5, min=0, max=120,
                                     tooltip="0 means not sent. MiniMax-H3 supports 4 to 15"),
                        io.String.Input("size", display_name="resolution", default="",
                                        placeholder="768P / 2K / 1280x720. Empty for default"),
                        io.Combo.Input("ratio", display_name="aspect ratio", default="auto",
                                       options=["auto", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"],
                                       tooltip="auto means not sent. adaptive follows the first frame, so it needs an image"),
                    ]),
                ]),
                seed_input(),
                io.Int.Input("poll_interval", display_name="poll interval (s)", default=5, min=1, max=120, advanced=True),
                timeout_input(1200),
                extra_input(),
            ],
            outputs=[io.Video.Output(display_name="video"), io.String.Output(display_name="video url")],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    async def execute(cls, relay, model, prompt, api_format, seed, poll_interval, timeout, extra_json,
                      image=None) -> io.NodeOutput:
        r = rc.get_relay(relay)
        model = _require(model, tr("model"))
        extra = rc.parse_extra(extra_json)
        first = _first_frame(image)
        fmt = api_format["api_format"]
        _show(cls, tr("Submitting the task…"))
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
            _show(cls, tr("Generating (video through the chat API usually takes a few minutes)…"))
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
                r, _require(api_format.get("submit_path"), tr("submit path")),
                (api_format.get("query_path") or "").strip(), rc.merge_extra(body, extra), **wait,
            )

        return io.NodeOutput(_to_video(video), url)


class RelayModels(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="RelayAPI_Models",
            display_name="Relay Models",
            category=CATEGORY,
            description="Lists the model names in the relay's /v1/models, ready to copy into other nodes.",
            search_aliases=["relay", "中转站", "models", "模型"],
            inputs=[
                relay_input(),
                io.String.Input("keyword", display_name="keyword", default="",
                                placeholder="Optional: image / video / gpt. Separate several with spaces"),
            ],
            outputs=[io.String.Output(display_name="models")],
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
        text = tr("{relay}: {count} models", relay=relay, count=len(ids)) + "\n" + ("\n".join(ids) if ids else tr("(no matching models)"))
        return io.NodeOutput(text, ui=ui.PreviewText(text))
