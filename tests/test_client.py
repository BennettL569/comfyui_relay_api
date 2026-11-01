"""relay_client 单元测试：对着本地模拟中转站把所有接口格式跑一遍。

    python -X utf8 tests/test_client.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
os.environ["COMFY_RELAY_LANG"] = "zh"  # 断言用的是中文原话，顺带核对翻译
os.environ["COMFY_RELAY_ALLOW_PRIVATE_DOWNLOADS"] = "1"  # 模拟的第三方存储在本机。关掉时的行为在 t_download_safety 里单独测

import relay_client as rc  # noqa: E402
from mock_relay import KEY, KEY_LIMITED, MP4, PNG, MockRelay  # noqa: E402

RELAY_PORT, CDN_PORT = 18911, 18912
WAIT = dict(poll_interval=0.2, timeout=30, check_interrupt=None)


def relay() -> rc.Relay:
    return rc.get_relay("mock")


def expect_error(fn, fragment: str) -> None:
    try:
        fn()
    except rc.RelayError as e:
        assert fragment in str(e), f"错误信息里没有「{fragment}」：{e}"
    else:
        raise AssertionError(f"应该报错（{fragment}）却没有")


async def expect_error_async(coro, fragment: str) -> None:
    try:
        await coro
    except rc.RelayError as e:
        assert fragment in str(e), f"错误信息里没有「{fragment}」：{e}"
    else:
        raise AssertionError(f"应该报错（{fragment}）却没有")


def write_config() -> str:
    """写一份测试用的 relays.json，返回它所在的临时目录。"""
    cfg = {
        "_说明": "测试用",
        "mock": {"base_url": f"http://127.0.0.1:{RELAY_PORT}/v1", "api_key": KEY},
        "nokey": {"base_url": f"http://127.0.0.1:{RELAY_PORT}", "api_key": "在这里填你的key"},
        "nourl": {"base_url": "https://你的中转站地址/v1", "api_key": KEY},
        "badkey": {"base_url": f"http://127.0.0.1:{RELAY_PORT}", "api_key": "sk-wrong"},
        "envkey": {"base_url": f"http://127.0.0.1:{RELAY_PORT}", "api_key_env": "RELAY_TEST_KEY"},
    }
    folder = tempfile.mkdtemp(prefix="relay_test_")
    path = os.path.join(folder, "relays.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    os.environ[rc.CONFIG_ENV] = path
    return folder


async def t_config(mock):
    assert set(rc.load_relays()) == {"mock", "nokey", "nourl", "badkey", "envkey"}
    expect_error(lambda: rc.get_relay("nokey"), "api_key 还没填")
    expect_error(lambda: rc.get_relay("nourl"), "base_url 还没填好")
    expect_error(lambda: rc.get_relay("missing"), "没有名为")
    os.environ["RELAY_TEST_KEY"] = KEY
    assert rc.get_relay("envkey").api_key == KEY
    r = relay()
    assert r.root == f"http://127.0.0.1:{RELAY_PORT}"
    assert r.url("/v1/models") == f"http://127.0.0.1:{RELAY_PORT}/v1/models"
    assert r.resolve("/files/a.mp4") == f"http://127.0.0.1:{RELAY_PORT}/files/a.mp4"
    assert r.same_origin(f"http://127.0.0.1:{RELAY_PORT}/x")
    assert not r.same_origin(f"http://127.0.0.1:{CDN_PORT}/x")


async def t_extra(mock):
    body = {"model": "m", "n": 2, "cfg": {"a": 1, "b": 2}}
    rc.merge_extra(body, {"n": None, "cfg": {"b": 3}, "q": "high"})
    assert body == {"model": "m", "cfg": {"a": 1, "b": 3}, "q": "high"}, body
    expect_error(lambda: rc.parse_extra("{bad"), "不是合法的 JSON")
    expect_error(lambda: rc.parse_extra("[1]"), "必须是 JSON 对象")
    assert rc.parse_extra("  ") == {}


async def t_parsers(mock):
    assert rc.find_video_url_in_text("完成：https://x.com/a.mp4。") == "https://x.com/a.mp4"
    assert rc.find_video_url_in_text("看这里 https://x.com/a.png") is None
    kling = {"code": 0, "request_id": "r", "data": {"task_id": "t", "task_status": "SUCCEED", "task_result": {
        "videos": [{"url": "https://c/v.mp4", "cover_url": "https://c/c.jpg"}]}}}
    assert rc.find_video_url(kling) == "https://c/v.mp4"
    assert rc.find_status(kling) == "succeed"
    assert rc.find_task_id(kling) == "t"
    vidu = {"state": "success", "creations": [{"cover_url": "https://c/c.jpg", "url": "https://c/v"}]}
    assert rc.find_video_url(vidu) == "https://c/v"
    seedance = {"id": "cgt-1", "status": "succeeded", "content": {"video_url": "https://c/v?sig=1"}}
    assert rc.find_video_url(seedance) == "https://c/v?sig=1"
    assert rc.find_progress({"progress": "45%"}) == 45.0
    assert rc.find_progress({"data": {"progress": 120}}) == 100.0
    assert rc.find_image_refs({"content": [{"type": "image_url", "image_url": {"url": "https://x/a"}}]}) == ["https://x/a"]
    assert rc.short("data:image/png;base64," + "A" * 1000).startswith("data:…")


async def t_models(mock):
    assert "gpt-image-1" in await rc.list_models(relay())
    await expect_error_async(rc.list_models(rc.get_relay("badkey")), "HTTP 401")


async def t_chat(mock):
    body = {"model": "mock-chat", "messages": [
        {"role": "system", "content": "S"},
        {"role": "user", "content": rc.chat_content("你好", [(PNG, "image/png")])},
    ]}
    text, _ = await rc.chat_completion(relay(), body, timeout=30, check_interrupt=None)
    assert text == "echo:你好|images:1|system:S", text
    streamed, _ = await rc.chat_completion(relay(), body, timeout=30, check_interrupt=None, stream=True)
    assert streamed == text, streamed
    bad = {"model": "chat-error-200", "messages": [{"role": "user", "content": "x"}]}
    await expect_error_async(rc.chat_completion(relay(), bad, timeout=30, check_interrupt=None), "余额不足")


async def t_images(mock):
    common = dict(timeout=30, check_interrupt=None)
    imgs, text = await rc.images_api(relay(), "gpt-image-1", "cat", [], size="1024x1024", n=2, quality="",
                                     ref_mode="edits", extra={"background": "transparent"}, **common)
    assert imgs == [PNG, PNG] and "rp:cat" in text
    body = mock.last("images_generations")
    assert body["size"] == "1024x1024" and body["n"] == 2 and body["background"] == "transparent"
    assert "quality" not in body
    refs = [(PNG, "image/png"), (PNG, "image/png")]
    await rc.images_api(relay(), "gpt-image-1", "edit", refs, size="", n=1, quality="high",
                        ref_mode="edits", extra={}, **common)
    info = mock.last("images_edits")
    assert [f[0] for f in info["files"]] == ["image[]", "image[]"], info
    assert info["fields"] == {"model": "gpt-image-1", "prompt": "edit", "quality": "high"}, info
    await rc.images_api(relay(), "gpt-image-1", "edit", refs[:1], size="", n=1, quality="",
                        ref_mode="edits", extra={}, **common)
    assert [f[0] for f in mock.last("images_edits")["files"]] == ["image"]
    await rc.images_api(relay(), "doubao-seedream-4-0", "x", refs, size="2K", n=1, quality="",
                        ref_mode="json", extra={"watermark": False}, **common)
    body = mock.last("images_generations")
    assert len(body["image"]) == 2 and body["image"][0].startswith("data:image/png;base64,")
    assert body["size"] == "2K" and body["watermark"] is False
    mock.cdn_auth.clear()
    imgs, _ = await rc.images_api(relay(), "img-url", "x", [], size="", n=1, quality="",
                                  ref_mode="edits", extra={}, **common)
    assert imgs == [PNG] and mock.cdn_auth == [None], mock.cdn_auth  # 第三方存储不能收到 Key


async def t_chat_image(mock):
    common = dict(extra={}, timeout=30, check_interrupt=None)
    for model in ("chat-image-md", "chat-image-field"):
        imgs, text = await rc.chat_image(relay(), model, "画猫", [(PNG, "image/png")], **common)
        assert imgs == [PNG], model
        assert "base64" not in text, text
    mock.cdn_auth.clear()
    imgs, _ = await rc.chat_image(relay(), "chat-image-link", "画猫", [], **common)
    assert imgs == [PNG] and mock.cdn_auth == [None]
    await expect_error_async(rc.chat_image(relay(), "chat-no-image", "画猫", [], **common), "没法画")


async def t_gemini(mock):
    common = dict(extra={}, timeout=30, check_interrupt=None)
    imgs, text = await rc.gemini_image(relay(), "models/gemini-3-pro-image-preview", "画猫", [(PNG, "image/png")],
                                       aspect_ratio="16:9", image_size="4K", **common)
    assert imgs == [PNG] and text == "这是最终结果", text  # 思考草稿要被跳过
    info = mock.last("gemini")
    assert info["path"] == "gemini-3-pro-image-preview:generateContent", info["path"]
    assert info["body"]["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "4K"}
    assert info["body"]["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "image/png"
    assert info["goog_key"] == KEY
    await rc.gemini_image(relay(), "gemini-3-pro-image-preview", "画猫", [], aspect_ratio="auto",
                          image_size="auto", **common)
    assert "imageConfig" not in mock.last("gemini")["body"]["generationConfig"]
    await expect_error_async(rc.gemini_image(relay(), "gemini-blocked", "x", [], aspect_ratio="auto",
                                             image_size="auto", **common), "SAFETY")


async def t_video_unified(mock):
    seen, statuses = [], []
    video, url = await rc.video_unified(relay(), {"model": "kling-v2-1", "prompt": "p", "duration": 5},
                                        on_progress=seen.append, on_status=statuses.append, **WAIT)
    assert video == MP4 and url == f"{mock.cdn}/files/test.mp4" and seen == [30.0, 60.0], (url, seen)
    assert statuses[0].startswith("视频任务：in_progress 30%，已等待") and "completed" in statuses[-1], statuses
    await expect_error_async(rc.video_unified(relay(), {"model": "video-fail", "prompt": "p"}, on_progress=None, **WAIT),
                             "内容审核未通过")


async def t_video_openai(mock):
    video, url = await rc.video_openai(relay(), {"model": "sora-2", "prompt": "p", "seconds": "4"},
                                       (PNG, "image/png"), multipart=True, on_progress=None, **WAIT)
    assert video == MP4 and url.endswith("/v1/videos/video_1/content"), url
    info = mock.last("openai_create")
    assert info["type"] == "multipart" and info["fields"]["seconds"] == "4", info
    assert info["files"][0][:2] == ("input_reference", "image/png"), info
    assert mock.last("openai_content")["auth"] == f"Bearer {KEY}"  # 同源下载要带 Key
    await rc.video_openai(relay(), {"model": "sora-2", "prompt": "p"}, None, multipart=True, on_progress=None, **WAIT)
    assert mock.last("openai_create")["type"] == "multipart"  # 没有文件也要用 multipart
    await rc.video_openai(relay(), {"model": "sora-2", "prompt": "p"}, (PNG, "image/png"), multipart=False,
                          on_progress=None, **WAIT)
    info = mock.last("openai_create")
    assert info["type"] == "json" and info["fields"]["input_reference"].startswith("data:image/png;base64,")


async def t_video_chat(mock):
    mock.cdn_auth.clear()
    body = {"model": "chat-video", "messages": [{"role": "user", "content": "p"}]}
    video, url = await rc.video_chat(relay(), body, stream=True, timeout=30, check_interrupt=None)
    assert video == MP4 and url == f"{mock.cdn}/files/test.mp4" and mock.cdn_auth == [None], url


async def t_video_custom(mock):
    body = rc.merge_extra({"model": "x", "prompt": "p", "image": "b64"}, {"model": None, "model_name": "kling-v2"})
    video, _ = await rc.video_custom(relay(), "/kling/v1/videos/image2video", "/kling/v1/videos/image2video/{task_id}",
                                     body, on_progress=None, **WAIT)
    assert video == MP4
    assert mock.last("kling_create") == {"prompt": "p", "image": "b64", "model_name": "kling-v2"}
    # 自定义路径填外部网址：令牌不能被带过去
    await expect_error_async(rc.video_custom(relay(), f"{mock.cdn}/steal", "", body, on_progress=None, **WAIT),
                             "只能请求中转站自己的地址")
    assert not any(a for a in mock.cdn_auth), "令牌被发到了外部网址"


async def t_video_task(mock):
    def submitted(before):
        return [info["plugin"] for name, info in mock.requests[before:] if name == "task_create"]

    # 没填插件键：提交到 /v1/videos，站点按模型找插件
    body = {"model": "MiniMax-H3", "prompt": "p", "duration": 5, "metadata": {"ratio": "16:9"}}
    seen, statuses = [], []
    video, url = await rc.video_task(relay(), "", body, on_progress=seen.append, on_status=statuses.append, **WAIT)
    assert video == MP4 and seen == [30.0, 50.0, 100.0], seen
    assert mock.last("task_create") == {"plugin": "", "body": body}
    # 取的是视频而不是封面，用带令牌的内容接口下载；content_url 里的凭据不能出现在输出里
    assert mock.last("task_content") == {"key": "clip", "auth": f"Bearer {KEY}"}
    assert url.endswith("/v1/tasks/task-1/artifacts/clip/content") and "secret" not in url, url
    assert statuses[0].startswith("视频任务：queued 30%") and "success" in statuses[-1], statuses
    # 填对了插件键：提交到 /v1/tasks/{插件键}
    plain = {"model": "wan 3.0-720p-30s", "prompt": "p"}
    video, _ = await rc.video_task(relay(), "custom-h3", plain, on_progress=None, **WAIT)
    assert video == MP4 and mock.last("task_create") == {"plugin": "custom-h3", "body": plain}
    # 插件键不对（站点上有这个键但模型不归它、站点上没有这个键）：站点在选渠道时就拒了，没扣费，换 /v1/videos 只提交一次
    for wrong in ("hailuo", "no-such"):
        before = len(mock.requests)
        video, _ = await rc.video_task(relay(), wrong, plain, on_progress=None, **WAIT)
        assert video == MP4 and submitted(before) == [wrong, ""], submitted(before)
    # 已经到了插件那一步的报错：不能换入口再提交一次，可能重复扣费
    before = len(mock.requests)
    await expect_error_async(rc.video_task(relay(), "custom-h3", {"model": "task-invalid", "prompt": "p"},
                                           on_progress=None, **WAIT), "requires a prompt")
    assert submitted(before) == ["custom-h3"], submitted(before)
    # 限定了模型的令牌查不了 /v1/videos/{id}，这条路查的是 /v1/tasks/{id}，不受影响
    limited = rc.Relay("limited", f"http://127.0.0.1:{RELAY_PORT}/v1", KEY_LIMITED)
    video, _ = await rc.video_task(limited, "", plain, on_progress=None, **WAIT)
    assert video == MP4
    # 查询偶尔失败不放弃，任务已经扣费了
    flaky_status = []
    video, _ = await rc.video_task(relay(), "", {"model": "task-flaky", "prompt": "p"}, on_progress=None,
                                   on_status=flaky_status.append, **WAIT)
    assert video == MP4 and "查询失败" in flaky_status[0], flaky_status
    # 任务失败：报原因和任务 ID
    fail = {"model": "task-fail", "prompt": "p"}
    await expect_error_async(rc.video_task(relay(), "", fail, on_progress=None, **WAIT), "内容审核未通过")
    await expect_error_async(rc.video_task(relay(), "", fail, on_progress=None, **WAIT), "任务 ID：task-fail-1")
    # 哪个入口都找不到渠道：提交时就报错，把站点的原话告诉用户
    no_channel = {"model": "task-no-channel", "prompt": "p"}
    await expect_error_async(rc.video_task(relay(), "", no_channel, on_progress=None, **WAIT), "无可用渠道")
    before = len(mock.requests)
    await expect_error_async(rc.video_task(relay(), "custom-h3", no_channel, on_progress=None, **WAIT), "无可用渠道")
    assert submitted(before) == ["custom-h3", ""], submitted(before)
    # 插件键来自工作流，不能借它请求别的路径
    before = len(mock.requests)
    video, _ = await rc.video_task(relay(), "../../chat/completions", plain, on_progress=None, **WAIT)
    assert video == MP4 and submitted(before) == ["../../chat/completions", ""], submitted(before)
    assert not any(name == "chat" for name, _ in mock.requests[before:])


async def t_download_safety(mock):
    r = relay()
    cdn = f"{mock.cdn}/files/test.png"
    own = f"http://127.0.0.1:{RELAY_PORT}"
    async with rc.new_session() as s:
        # 回复里的地址不可信：不是中转站自己的，就不能指向本机和内网
        del os.environ[rc.ALLOW_PRIVATE_ENV]
        try:
            for url in (cdn, "http://127.0.0.1:9/x.png", "http://[::1]:9/x.png", "http://169.254.169.254/latest/meta-data",
                        "http://10.0.0.1/x.png", "http://192.168.1.1/x.png", "http://localhost:9/x.png"):
                await expect_error_async(rc.download(s, r, url), "拒绝从")
            # 中转站自己的地址不受影响，哪怕它就在本机
            assert await rc.download(s, r, f"{own}/owned/test.png") == PNG
            # 中转站把下载重定向到内网：也要拒绝，每一跳都要查
            await expect_error_async(rc.download(s, r, f"{own}/redirect?to={cdn}"), "拒绝从")
        finally:
            os.environ[rc.ALLOW_PRIVATE_ENV] = "1"

        # 重定向到第三方存储（常见的做法）：跟过去，但不带令牌
        mock.cdn_auth.clear()
        mock.cdn_goog.clear()
        assert await rc.download(s, r, f"{own}/redirect?to={cdn}") == PNG
        assert mock.cdn_auth == [None] and mock.cdn_goog == [None], (mock.cdn_auth, mock.cdn_goog)

        # 第三方地址的错误内容不能出现在报错里：等于让人读到那个地址返回了什么
        try:
            await rc.download(s, r, f"{mock.cdn}/files/secret.json")
        except rc.RelayError as e:
            assert "404" in str(e) and "internal-secret" not in str(e), e
        else:
            raise AssertionError("应该报错")

        # 带令牌的接口请求不跟随去别的站点的重定向，令牌留在这个站
        mock.cdn_auth.clear()
        mock.cdn_goog.clear()
        await expect_error_async(rc.request_json(s, r, "GET", f"/redirect?to={mock.cdn}/files/test.png"), "重定向")
        await expect_error_async(rc.request_json(s, r, "POST", f"/redirect?to={mock.cdn}/files/test.png", json_body={}), "重定向")
        assert mock.cdn_auth == [] and mock.cdn_goog == [], "令牌被带到了别的站点"
        # 同一个站点内的 GET 可以跟
        assert (await rc.request_json(s, r, "GET", f"/redirect?to={own}/v1/models"))["success"] is True

    # 工作流里的模型名不能改变 Gemini 的请求路径，也不能把 x-goog-api-key 带走
    mock.cdn_goog.clear()
    evil = f"../../redirect?to={mock.cdn}/files/test.png#"
    try:
        await rc.gemini_image(r, evil, "p", [], aspect_ratio="auto", image_size="auto", extra={}, timeout=10, check_interrupt=None)
    except rc.RelayError:
        pass
    assert mock.cdn_goog == [], "令牌被带到了别的站点"

    # 地址判断：各种写法的内网都算内网，Clash 这类工具的假 IP 不算
    for address in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "::1", "::ffff:127.0.0.1",
                    "64:ff9b::a00:1", "::7f00:1", "fec0::1", "fd00::1", "ff02::1", "224.0.0.1", "2002:7f00:1::1", "bad"):
        assert not rc._is_public_ip(address), address
    for address in ("8.8.8.8", "2001:4860:4860::8888", "198.18.0.5", "198.19.255.1", "fc00::5", "64:ff9b::808:808"):
        assert rc._is_public_ip(address), address
    # 回复里的地址写法不对：报错，不能让异常把任务 ID 弄丢
    async with rc.new_session() as s:
        for url in ("http://[bad/x.png", "http://a..b/x.png", "http://example.com:99999/x.png"):
            await expect_error_async(rc.download(s, r, url), "下载失败")

    # 只设了 NO_PROXY：不算走代理，连接时的检查还要有
    saved = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "example.com"
    try:
        session = rc._external_session(r)
        try:
            assert isinstance(session.connector._resolver, rc._PublicResolver) or "HTTP_PROXY" in os.environ or "HTTPS_PROXY" in os.environ
        finally:
            await session.close()
    finally:
        if saved is None:
            del os.environ["NO_PROXY"]
        else:
            os.environ["NO_PROXY"] = saved

    # 一条回复里的图片地址有上限
    many = {"content": " ".join(f"https://a.example.com/{i}.png" for i in range(100))}
    assert len(rc.find_image_refs(many)) == rc.MAX_IMAGE_REFS


async def t_secrets_in_errors(mock):
    # 代理账号密码单独传，连接失败的报错里不能出现
    proxied = rc.Relay("p", "https://relay.invalid/v1", KEY, "http://alice:s3cretpw@127.0.0.1:9")
    assert proxied.proxy_args["proxy"] == "http://127.0.0.1:9" and proxied.proxy_args["proxy_auth"].password == "s3cretpw"
    async with rc.new_session() as s:
        try:
            await rc.request_json(s, proxied, "GET", "/v1/models", timeout=10)
        except rc.RelayError as e:
            assert "s3cretpw" not in str(e) and "alice" not in str(e) and KEY not in str(e), e
        else:
            raise AssertionError("应该报错")
    assert rc.Relay("p", "https://r.example.com", "k").proxy_args == {"proxy": None, "proxy_auth": None}
    # 代理回 407 时 aiohttp 的报错会带上代理地址
    async def answer(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 18913)
    try:
        strict = rc.Relay("p", "https://relay.invalid/v1", KEY, "http://alice:s3cretpw@127.0.0.1:18913")
        async with rc.new_session() as s:
            try:
                await rc.request_json(s, strict, "GET", "/v1/models", timeout=10)
            except rc.RelayError as e:
                assert "s3cretpw" not in str(e) and "alice" not in str(e), e
            else:
                raise AssertionError("应该报错")
    finally:
        server.close()
        await server.wait_closed()
    # 报错文字里的令牌也会被去掉
    assert KEY not in relay().scrub(f"bad {KEY}")
    # 太短的代理用户名不替换，不然报错里的字母都变成 ***
    assert rc.Relay("p", "https://r.example.com", "k" * 20, "http://a:longpassword@127.0.0.1:9").scrub("bad address longpassword") == "bad address ***"


async def t_poll_failures(mock):
    # 限定了模型的令牌：能提交，查询一直 403。重试几次后报错，并带上任务 ID 方便到后台找
    limited = rc.Relay("limited", f"http://127.0.0.1:{RELAY_PORT}/v1", KEY_LIMITED)
    try:
        await rc.video_unified(limited, {"model": "kling-v2-1", "prompt": "p"}, on_progress=None, **WAIT)
    except rc.RelayError as e:
        assert "任务 ID：u-1" in str(e) and "无权访问模型" in str(e), e
    else:
        raise AssertionError("查询一直失败应该报错")


async def t_interrupt(mock):
    class Stop(Exception):
        pass

    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls > 3:
            raise Stop

    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        await rc.video_unified(relay(), {"model": "kling-v2-1", "prompt": "p"}, poll_interval=5, timeout=60,
                               check_interrupt=check, on_progress=None)
    except Stop:
        pass
    else:
        raise AssertionError("没有响应取消")
    assert loop.time() - start < 4, "取消响应太慢"


TESTS = [t_config, t_extra, t_parsers, t_models, t_chat, t_images, t_chat_image, t_gemini,
         t_video_unified, t_video_openai, t_video_chat, t_video_custom, t_video_task, t_download_safety, t_secrets_in_errors, t_poll_failures, t_interrupt]


async def main() -> int:
    config_dir = write_config()
    mock = MockRelay(RELAY_PORT, CDN_PORT)
    await mock.start()
    failed = 0
    try:
        for test in TESTS:
            try:
                await test(mock)
                print(f"PASS  {test.__name__}")
            except Exception:
                failed += 1
                print(f"FAIL  {test.__name__}")
                traceback.print_exc()
    finally:
        await mock.stop()
        shutil.rmtree(config_dir, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} 通过")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
