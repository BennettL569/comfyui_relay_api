"""端到端测试：用本机 ComfyUI 起一个隔离的无界面实例（独立端口、临时目录、CPU 模式），
加载插件后对着模拟中转站跑真实工作流。不碰你平时用的 input/output/user 目录和 custom_nodes。

用运行 ComfyUI 的 Python 运行：
    python -X utf8 tests/test_e2e.py <ComfyUI 目录>
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mock_relay import KEY, MockRelay, make_png  # noqa: E402

PLUGIN = os.path.dirname(HERE)
RELAY_PORT, CDN_PORT, COMFY_PORT = 18921, 18922, 18923
COMFY = f"http://127.0.0.1:{COMFY_PORT}"


def prepare(base: str) -> str:
    for d in ("custom_nodes", "input", "output", "user", "temp"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    shutil.copytree(PLUGIN, os.path.join(base, "custom_nodes", "comfyui_relay_api"),
                    ignore=shutil.ignore_patterns("relays.json", "relays.json.*", ".relays-*", "__pycache__", ".git", "tests"))
    with open(os.path.join(base, "input", "ref.png"), "wb") as f:
        f.write(make_png((40, 160, 90), (96, 64)))
    config = os.path.join(base, "relays.json")
    with open(config, "w", encoding="utf-8") as f:
        json.dump({
            "mock": {"base_url": f"http://127.0.0.1:{RELAY_PORT}/v1", "api_key": KEY},
            "备用站": {"base_url": f"http://127.0.0.1:{RELAY_PORT}", "api_key": KEY},
        }, f, ensure_ascii=False)
    return config


def launch(comfy_dir: str, base: str, config: str, log_path: str) -> subprocess.Popen:
    # 断言用的是中文原话，顺带核对翻译
    env = dict(os.environ, COMFY_RELAY_CONFIG=config, COMFY_RELAY_LANG="zh", COMFY_RELAY_ALLOW_PRIVATE_DOWNLOADS="1",  # 模拟的第三方存储在本机
               PYTHONIOENCODING="utf-8")
    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "main.py", "--listen", "127.0.0.1", "--port", str(COMFY_PORT), "--cpu", "--disable-auto-launch",
         "--base-directory", base, "--database-url", "sqlite:///:memory:"],
        cwd=comfy_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


async def wait_ready(session: aiohttp.ClientSession, proc: subprocess.Popen, timeout: float = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"ComfyUI 进程退出了（code {proc.returncode}）")
        try:
            async with session.get(f"{COMFY}/object_info/KSampler") as r:
                if r.status == 200 and await r.json():
                    return
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(1)
    raise RuntimeError("等待 ComfyUI 启动超时")


async def run(session: aiohttp.ClientSession, workflow: dict) -> dict:
    async with session.post(f"{COMFY}/prompt", json={"prompt": workflow, "client_id": "relay-e2e"}) as r:
        data = await r.json()
        if r.status != 200:
            raise AssertionError(f"工作流校验没通过：{json.dumps(data, ensure_ascii=False)[:3000]}")
    pid = data["prompt_id"]
    for _ in range(1200):
        async with session.get(f"{COMFY}/history/{pid}") as r:
            hist = await r.json()
        if pid in hist:
            return hist[pid]
        await asyncio.sleep(0.25)
    raise AssertionError("工作流执行超时")


def error_message(entry: dict) -> str:
    for kind, info in entry["status"].get("messages", []):
        if kind == "execution_error":
            return info.get("exception_message", "")
    return ""


def assert_ok(entry: dict) -> None:
    status = entry["status"]["status_str"]
    assert status == "success", f"执行失败：{error_message(entry)}"


def load_image(node_inputs: dict) -> dict:
    return {"1": {"class_type": "LoadImage", "inputs": {"image": "ref.png"}}, **node_inputs}


def image_wf(model: str, fmt: dict, prefix: str, with_ref: bool = True) -> dict:
    inputs = {"relay": "mock", "model": model, "prompt": "画一只猫", "seed": 1, "extra_json": "", "timeout": 60, **fmt}
    wf = {"2": {"class_type": "RelayAPI_Image", "inputs": inputs},
          "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": f"relay/{prefix}"}}}
    if with_ref:
        inputs["images"] = ["1", 0]
        wf = load_image(wf)
    return wf


def video_wf(model: str, fmt: dict, prefix: str, extra: str = "", with_ref: bool = True) -> dict:
    inputs = {"relay": "mock", "model": model, "prompt": "猫在草地上跑", "seed": 1, "poll_interval": 1,
              "timeout": 60, "extra_json": extra, **fmt}
    wf = {"5": {"class_type": "RelayAPI_Video", "inputs": inputs},
          "6": {"class_type": "SaveVideo", "inputs": {"video": ["5", 0], "filename_prefix": f"relay/{prefix}",
                                                      "format": "auto", "format.codec": "auto"}}}
    if with_ref:
        inputs["image"] = ["1", 0]
        wf = load_image(wf)
    return wf


async def main(comfy_dir: str) -> int:
    base = tempfile.mkdtemp(prefix="relay_e2e_")
    log_path = os.path.join(base, "comfyui.log")
    config = prepare(base)
    out_dir = os.path.join(base, "output", "relay")
    mock = MockRelay(RELAY_PORT, CDN_PORT)
    await mock.start()
    proc = launch(comfy_dir, base, config, log_path)
    failed = 0

    async def case(name, coro):
        nonlocal failed
        try:
            await coro
            print(f"PASS  {name}")
        except Exception:
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()

    try:
        async with aiohttp.ClientSession() as s:
            started = time.time()
            await wait_ready(s, proc)
            print(f"ComfyUI 已启动（{time.time() - started:.0f} 秒）")

            async def nodes_registered():
                async with s.get(f"{COMFY}/object_info") as r:
                    info = await r.json()
                for node in ("RelayAPI_Chat", "RelayAPI_Image", "RelayAPI_Video", "RelayAPI_Models"):
                    assert node in info, f"{node} 没有注册，插件加载失败"
                relay_spec = info["RelayAPI_Image"]["input"]["required"]["relay"]
                assert relay_spec[1]["options"] == ["mock", "备用站"] or relay_spec[0] == ["mock", "备用站"], relay_spec
                assert info["RelayAPI_Image"]["category"] == "API Relay"
                assert info["RelayAPI_Image"]["display_name"] == "Relay Image", "节点名默认英文，中文走语言包"
                # 语言包要覆盖节点的每个输入和输出
                with open(os.path.join(PLUGIN, "locales", "zh", "nodeDefs.json"), encoding="utf-8") as f:
                    node_defs = json.load(f)
                for node, names in node_defs.items():
                    inputs = {**info[node]["input"]["required"], **info[node]["input"].get("optional", {})}
                    missing = [k for k in inputs if k not in names["inputs"]]
                    assert not missing, f"{node} 的语言包缺少 {missing}"
                    assert len(names["outputs"]) == len(info[node]["output"]), node
            await case("节点注册与中转站下拉框", nodes_registered())

            async def chat():
                wf = {"1": {"class_type": "RelayAPI_Chat", "inputs": {
                    "relay": "mock", "model": "mock-chat", "prompt": "你好", "system": "sys", "temperature": -1.0,
                    "max_tokens": 0, "seed": 1, "extra_json": "", "timeout": 60}}}
                entry = await run(s, wf)
                assert_ok(entry)
                assert entry["outputs"]["1"]["text"] == ["echo:你好|images:0|system:sys"], entry["outputs"]
            await case("文本对话", chat())

            async def chat_with_image():
                wf = load_image({"2": {"class_type": "RelayAPI_Chat", "inputs": {
                    "relay": "备用站", "model": "mock-chat", "prompt": "描述图片", "system": "", "images": ["1", 0],
                    "temperature": 0.5, "max_tokens": 100, "seed": 1, "extra_json": '{"top_p": 0.9}', "timeout": 60}}})
                entry = await run(s, wf)
                assert_ok(entry)
                assert entry["outputs"]["2"]["text"] == ["echo:描述图片|images:1|system:"], entry["outputs"]
                body = mock.last("chat")
                assert body["temperature"] == 0.5 and body["max_tokens"] == 100 and body["top_p"] == 0.9, body
            await case("识图（带图片的对话）+ 额外参数", chat_with_image())

            async def image_openai():
                wf = image_wf("gpt-image-1", {"api_format": "images", "api_format.size": "1024x1024",
                                              "api_format.n": 1, "api_format.quality": "high"}, "openai")
                assert_ok(await run(s, wf))
                info = mock.last("images_edits")
                assert [f[0] for f in info["files"]] == ["image"] and info["fields"]["quality"] == "high", info
                assert glob.glob(os.path.join(out_dir, "openai_*.png"))
            await case("生图：images 格式 + 参考图（edits）", image_openai())

            async def image_json():
                wf = image_wf("doubao-seedream-4-0", {"api_format": "images-json", "api_format.size": "2K"}, "seedream")
                assert_ok(await run(s, wf))
                body = mock.last("images_generations")
                assert body["image"].startswith("data:image/png;base64,") and body["size"] == "2K", body.keys()
                assert glob.glob(os.path.join(out_dir, "seedream_*.png"))
            await case("生图：images-json 格式（Seedream 类）", image_json())

            async def image_chat():
                wf = image_wf("chat-image-md", {"api_format": "chat"}, "chat", with_ref=False)
                assert_ok(await run(s, wf))
                assert glob.glob(os.path.join(out_dir, "chat_*.png"))
            await case("生图：chat 格式（nano-banana 类）", image_chat())

            async def image_gemini():
                wf = image_wf("gemini-3-pro-image-preview", {"api_format": "gemini", "api_format.aspect_ratio": "16:9",
                                                             "api_format.image_size": "2K"}, "gemini")
                assert_ok(await run(s, wf))
                body = mock.last("gemini")["body"]
                assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
                assert "inline_data" in body["contents"][0]["parts"][0]
                assert glob.glob(os.path.join(out_dir, "gemini_*.png"))
            await case("生图：gemini 原生格式", image_gemini())

            async def video_unified():
                wf = video_wf("kling-v2-1", {"api_format": "unified", "api_format.duration": 5,
                                             "api_format.size": "1280x720", "api_format.image_encoding": "data_url"},
                              "unified")
                entry = await run(s, wf)
                assert_ok(entry)
                body = mock.last("unified_create")
                assert (body["width"], body["height"], body["duration"]) == (1280, 720, 5), body.keys()
                assert body["image"].startswith("data:image/png;base64,")
                assert glob.glob(os.path.join(out_dir, "unified_*.mp4"))
            await case("生视频：unified 格式 + 首帧图", video_unified())

            async def video_openai():
                wf = video_wf("sora-2", {"api_format": "openai", "api_format.seconds": 4, "api_format.size": "64x48",
                                         "api_format.body_format": "multipart"}, "sora")
                assert_ok(await run(s, wf))
                info = mock.last("openai_create")
                assert info["fields"]["seconds"] == "4" and info["files"][0][2] == (64, 48), info  # 参考图被裁成视频尺寸
                assert glob.glob(os.path.join(out_dir, "sora_*.mp4"))
            await case("生视频：openai/Sora 格式", video_openai())

            async def video_chat():
                wf = video_wf("chat-video", {"api_format": "chat", "api_format.stream": True}, "chatvideo", with_ref=False)
                assert_ok(await run(s, wf))
                assert glob.glob(os.path.join(out_dir, "chatvideo_*.mp4"))
            await case("生视频：chat 格式", video_chat())

            async def video_custom():
                wf = video_wf("kling-v2-1", {
                    "api_format": "custom", "api_format.submit_path": "/kling/v1/videos/image2video",
                    "api_format.query_path": "/kling/v1/videos/image2video/{task_id}",
                    "api_format.image_field": "image", "api_format.image_encoding": "base64",
                }, "kling", extra='{"model": null, "model_name": "kling-v2-1", "duration": "5"}')
                assert_ok(await run(s, wf))
                body = mock.last("kling_create")
                assert "model" not in body and body["model_name"] == "kling-v2-1" and body["duration"] == "5"
                assert not body["image"].startswith("data:")
                assert glob.glob(os.path.join(out_dir, "kling_*.mp4"))
            await case("生视频：custom 自定义路径（可灵原生格式）", video_custom())

            async def video_task():
                # 任务插件留空：站点按模型找插件
                wf = video_wf("MiniMax-H3", {"api_format": "task", "api_format.plugin_key": "",
                                             "api_format.duration": 5, "api_format.size": "768P",
                                             "api_format.ratio": "16:9"}, "h3")
                assert_ok(await run(s, wf))
                info = mock.last("task_create")
                body = info["body"]
                assert info["plugin"] == "" and body["model"] == "MiniMax-H3" and body["duration"] == 5, info
                assert body["size"] == "768P" and body["metadata"]["ratio"] == "16:9"
                assert body["metadata"]["first_frame_image"].startswith("data:image/png;base64,")
                assert glob.glob(os.path.join(out_dir, "h3_*.mp4"))
                # 文生视频：不发首帧图，宽高比 auto 也不发，让站点按默认来
                wf = video_wf("MiniMax-H3", {"api_format": "task", "api_format.plugin_key": " ",
                                             "api_format.duration": 0, "api_format.size": "",
                                             "api_format.ratio": "auto"}, "h3text", with_ref=False)
                assert_ok(await run(s, wf))
                info = mock.last("task_create")
                assert info["plugin"] == "" and set(info["body"]) == {"model", "prompt"}, info
                # 模型名带空格；任务插件填错了也能出片（真实站点上官方的 hailuo 就不管这些模型）
                wf = video_wf("wan 3.0-720p-30s", {"api_format": "task", "api_format.plugin_key": "hailuo",
                                                   "api_format.duration": 0, "api_format.size": "",
                                                   "api_format.ratio": "auto"}, "wan")
                assert_ok(await run(s, wf))
                info = mock.last("task_create")
                assert info["plugin"] == "" and info["body"]["model"] == "wan 3.0-720p-30s", info
                assert info["body"]["images"][0].startswith("data:image/png;base64,")
                assert glob.glob(os.path.join(out_dir, "wan_*.mp4"))
            await case("生视频：task 任务插件模型（MiniMax-H3、wan 3.0）", video_task())

            async def video_failure():
                wf = video_wf("video-fail", {"api_format": "unified", "api_format.duration": 0, "api_format.size": "",
                                             "api_format.image_encoding": "data_url"}, "fail", with_ref=False)
                entry = await run(s, wf)
                assert entry["status"]["status_str"] == "error", entry["status"]
                assert "内容审核未通过" in error_message(entry), error_message(entry)
            await case("生视频：任务失败时把原因报给用户", video_failure())

            async def models():
                wf = {"1": {"class_type": "RelayAPI_Models", "inputs": {"relay": "备用站", "keyword": "image"}}}
                entry = await run(s, wf)
                assert_ok(entry)
                text = entry["outputs"]["1"]["text"][0]
                assert "gpt-image-1" in text and "gpt-4o-mini" not in text, text
            await case("模型列表", models())

            async def manager_routes():
                async with s.get(f"{COMFY}/api/relay_api/state") as r:  # 前端 api.fetchApi 走 /api 前缀
                    data = await r.json()
                    assert r.status == 200 and [x["name"] for x in data["relays"]] == ["mock", "备用站"], data
                    assert KEY not in json.dumps(data), "接口里不能出现完整令牌"
                    assert data["can_edit"] is False, "ComfyUI 的接口默认不能改配置"
                async with s.post(f"{COMFY}/relay_api/relays", json={"name": "x", "base_url": "https://a.example.com", "api_key": "k"}) as r:
                    assert r.status == 403, "ComfyUI 的接口默认不能改配置"
                async with s.post(f"{COMFY}/relay_api/test", json={"base_url": "https://a.example.com", "api_key": "k"}) as r:
                    assert r.status == 403, "ComfyUI 的接口默认不能访问请求里给的地址"
                async with s.post(f"{COMFY}/relay_api/panel_edit", json={"enabled": True}) as r:
                    assert r.status in (404, 405), "ComfyUI 里不能有打开这个开关的接口"
                async with s.get(f"{COMFY}/relay_api/models/mock") as r:
                    data = await r.json()
                    assert data["ok"] and data["counts"]["image"] >= 2, data
                async with s.get(f"{COMFY}/relay_api/ping") as r:
                    assert (await r.json()) == {"ok": True, "app": "comfyui"}
                async with s.post(f"{COMFY}/relay_api/notify", json={"type": "relays-changed"}) as r:
                    assert r.status == 200
                async with s.post(f"{COMFY}/relay_api/notify", json={"type": "add-node", "node": "KSampler",
                                                                     "relay": "mock", "model": "x"}) as r:
                    assert r.status == 400
                async with s.get(f"{COMFY}/relay_api/state", headers={"Host": "evil.example.com"}) as r:
                    assert r.status == 403, "域名访问要被拒绝（防 DNS 重绑定）"
                async with s.get(f"{COMFY}/relay_api/manager/") as r:
                    assert r.status == 200 and "Relay Manager" in await r.text()
                async with s.get(f"{COMFY}/extensions") as r:
                    exts = await r.json()
                    assert "/extensions/comfyui_relay_api/relay_manager.js" in exts and not any("manager/" in e for e in exts)
            await case("管理器接口（在 ComfyUI 里）", manager_routes())
    except Exception:
        failed += 1
        traceback.print_exc()
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        await mock.stop()
        if failed:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                print("---- ComfyUI 日志末尾 ----\n" + "".join(f.readlines()[-60:]))
            print(f"临时目录保留在：{base}")
        else:
            shutil.rmtree(base, ignore_errors=True)
    print(f"\n失败 {failed} 项" if failed else "\n全部通过")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
