"""中转站管理器测试：配置读写、粘贴识别、模型分类、HTTP 接口（含独立模式的令牌和 Host 校验）。

    python -X utf8 tests/test_manager.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import traceback

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PLUGIN)

import manager_api  # noqa: E402
import manager_app  # noqa: E402
import relay_client as rc  # noqa: E402
import relay_probe as rp  # noqa: E402
import relay_store as rs  # noqa: E402
from mock_relay import KEY, KEY_LIMITED, MockRelay  # noqa: E402

RELAY_PORT, CDN_PORT = 18941, 18942
MOCK_URL = f"http://127.0.0.1:{RELAY_PORT}/v1"


def expect_error(fn, fragment: str) -> None:
    try:
        fn()
    except rc.RelayError as e:
        assert fragment in str(e), f"错误信息里没有「{fragment}」：{e}"
    else:
        raise AssertionError(f"应该报错（{fragment}）却没有")


def write_config(data: dict) -> str:
    with open(rc.config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return rc.config_path()


def read_config() -> dict:
    with open(rc.config_path(), encoding="utf-8") as f:
        return json.load(f)


# ---------- 配置读写 ----------

async def t_store_crud(mock):
    write_config({"_说明": "x", "站点1": {"base_url": "https://你的中转站地址/v1", "api_key": "在这里填你的key", "proxy": ""}})
    assert rs.remove_placeholders() == ["站点1"]
    assert list(read_config()) == ["_说明"], "说明字段要保留"

    view = rs.add_relay("主站", "https://api.a.com/v1/chat/completions", "sk-aaaaaaaaaaaaaaaaaaaa", "")
    assert view["base_url"] == "https://api.a.com/v1" and view["key_masked"] == "sk-aaa…aaaa" and "api_key" not in view
    rs.add_relay("备用", "api.b.com", "sk-bbbbbbbbbbbbbbbbbbbb", "http://127.0.0.1:7890")
    assert [r["name"] for r in rs.list_relays()] == ["主站", "备用"]
    expect_error(lambda: rs.add_relay("主站", "https://x.com", "sk-x", ""), "已经有叫")
    expect_error(lambda: rs.add_relay("新", "", "sk-x", ""), "接口地址")
    expect_error(lambda: rs.add_relay("新", "https://x.com", "", ""), "令牌")
    expect_error(lambda: rs.add_relay("a/b", "https://x.com", "sk-x", ""), "斜杠")
    expect_error(lambda: rs.add_relay("新", "https://x.com", "sk-x", "ftp://bad"), "只支持 HTTP 代理")

    # 令牌留空：地址不变时沿用原令牌；改名保持位置
    rs.update_relay("主站", "主站改名", "https://api.a.com/v1", "", "")
    cfg = read_config()
    assert list(cfg)[1:] == ["主站改名", "备用"] and cfg["主站改名"]["api_key"] == "sk-aaaaaaaaaaaaaaaaaaaa"
    # 改了地址却不填令牌：拒绝（防止令牌被发到别的地址）
    expect_error(lambda: rs.update_relay("主站改名", "主站改名", "https://evil.example.com/v1", "", ""), "重新填写令牌")
    rs.update_relay("主站改名", "主站改名", "https://api.c.com/v1", "sk-cccccccccccccccccccc", "")
    assert read_config()["主站改名"]["base_url"] == "https://api.c.com/v1"
    expect_error(lambda: rs.update_relay("不存在", "x", "https://x.com", "sk-x", ""), "没有名为")
    expect_error(lambda: rs.update_relay("备用", "主站改名", "https://api.b.com", "", "http://127.0.0.1:7890"), "已经有叫")
    # 只改代理也要重填令牌（否则 http 站点的令牌会经过别人的代理）
    expect_error(lambda: rs.update_relay("备用", "备用", "https://api.b.com", "", "http://evil.example.com:8080"),
                 "重新填写令牌")
    rs.update_relay("备用", "备用", "https://api.b.com", "", "http://127.0.0.1:7890")  # 什么都没改：可以沿用

    rs.delete_relay("备用")
    assert "备用" not in read_config()
    assert os.path.isfile(rc.config_path() + ".bak"), "保存前要备份旧文件"
    leftovers = [f for f in os.listdir(os.path.dirname(rc.config_path())) if f.startswith(".relays-")]
    assert not leftovers, f"临时文件没清理：{leftovers}"


async def t_parse(mock):
    cases = [
        ("API 地址：https://api.closeai.com/v1  令牌：sk-AbCdEfGhIjKlMnOpQrStUvWx", ["closeai"],
         {"name": "closeai2", "base_url": "https://api.closeai.com/v1", "api_key": "sk-AbCdEfGhIjKlMnOpQrStUvWx"}),
        ("https://api.xxx.com/v1/chat/completions\nsk-1234567890abcdefghijklmn", [],
         {"name": "xxx", "base_url": "https://api.xxx.com/v1"}),
        ("官网 https://www.xxx.cn  接口地址 https://api.xxx.cn  key: sk-zzzzzzzzzzzzzzzzzzzzzzzz", [],
         {"base_url": "https://api.xxx.cn"}),
        ("api.foo.com.cn/v1 sk-aaaaaaaaaaaaaaaaaaaaaaaa", [], {"name": "foo", "base_url": "https://api.foo.com.cn/v1"}),
        ("控制台 https://foo.top/console/token 令牌 sk-bbbbbbbbbbbbbbbbbbbbbbbb", [], {"base_url": "https://foo.top"}),
        ("base_url=https://r.example.org:8443/openai/v1/ key=abcd1234efgh5678ijkl9012mnop 代理: http://127.0.0.1:7890", [],
         {"base_url": "https://r.example.org:8443/openai/v1", "api_key": "abcd1234efgh5678ijkl9012mnop",
          "proxy": "http://127.0.0.1:7890"}),
        ("联系邮箱 admin@foo.com，令牌 sk-cccccccccccccccccccccccc", [], {"base_url": "", "api_key": "sk-cccccccccccccccccccccccc"}),
    ]
    key = "sk-AbCdEfGhIjKlMnOpQrStUvWx1234"
    cases += [
        # 网址后面直接跟中文、逗号：令牌不能被吞进接口地址
        (f"接口地址：https://api.closeai.com/v1令牌：{key}", [], {"base_url": "https://api.closeai.com/v1", "api_key": key}),
        (f"API地址:https://api.closeai.com/v1,密钥:{key}", [], {"base_url": "https://api.closeai.com/v1", "api_key": key}),
        (f"地址“https://api.x.com/v1”，令牌为{key}", [], {"base_url": "https://api.x.com/v1", "api_key": key}),
        (f"api.x.com 代理地址：http://127.0.0.1:7890 令牌 {key}", [],
         {"base_url": "https://api.x.com", "proxy": "http://127.0.0.1:7890"}),
        (f"https://api.x.com/v1 代理 127.0.0.1:10809 {key}", [], {"proxy": "http://127.0.0.1:10809"}),
    ]
    for text, existing, expected in cases:
        got = rs.parse_quick_text(text, existing)
        for k, value in expected.items():
            assert got[k] == value, f"{text!r}: {k}={got[k]!r}，应为 {value!r}"
    assert rs.normalize_base_url("https://relay.example.com/tokenhub/v1") == "https://relay.example.com/tokenhub/v1"
    assert rs.normalize_base_url("https://x.com/console/token") == "https://x.com"
    assert rs.normalize_base_url("localhost:3000/v1") == "http://localhost:3000/v1"
    assert rs.normalize_base_url("192.168.1.5:3000") == "http://192.168.1.5:3000"
    assert rs.normalize_base_url("API.X.com/v1") == "https://api.x.com/v1"
    expect_error(lambda: rs.check_base_url(f"https://api.x.com/v1{key}", key), "混进了")
    expect_error(lambda: rs.check_base_url("https://api.x.com/v1令牌"), "混进了")
    expect_error(lambda: rs.check_proxy("socks5://127.0.0.1:10808"), "只支持 HTTP 代理")
    assert rs.check_proxy("127.0.0.1:7890") == "http://127.0.0.1:7890"


async def t_store_edge(mock):
    """手动编辑出来的各种配置文件，以及编辑时令牌能不能沿用。"""
    path = rc.config_path()
    data = {"主站": {"base_url": "https://api.a.com/v1", "api_key": "sk-aaaaaaaaaaaaaaaaaaaa", "proxy": ""}, "备注": "我的笔记"}
    for encoding in ("gbk", "utf-16"):
        with open(path, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False)
        assert list(rc.load_relays()) == ["主站"], encoding
    with open(path, "wb") as f:
        f.write(b"\xff\xff\xfe garbage")
    expect_error(rc.load_relays, "编码")
    write_config(data)
    expect_error(lambda: rs.update_relay("主站", "备注", "https://api.a.com/v1", "", ""), "其他字段重名")
    assert read_config()["备注"] == "我的笔记" and "主站" in read_config()

    # 代理密码在界面上遮挡；原样提交回来表示没改
    rs.update_relay("主站", "主站", "https://api.a.com/v1", "sk-aaaaaaaaaaaaaaaaaaaa", "http://alice:S3cret@10.0.0.5:3128")
    view = rs.list_relays()[0]
    assert view["proxy"] == "http://alice:•••@10.0.0.5:3128" and "S3cret" not in json.dumps(view)
    rs.update_relay("主站", "主站", "https://api.a.com/v1", "", view["proxy"])
    assert read_config()["主站"]["proxy"] == "http://alice:S3cret@10.0.0.5:3128"
    # 同一个站点去掉 /v1、去掉代理：可以沿用令牌；换协议、换代理：要重填
    rs.update_relay("主站", "主站", "https://api.a.com", "", "")
    expect_error(lambda: rs.update_relay("主站", "主站", "http://api.a.com", "", ""), "重新填写令牌")
    expect_error(lambda: rs.update_relay("主站", "主站", "https://api.a.com", "", "http://10.0.0.9:3128"), "重新填写令牌")
    assert read_config()["主站"]["api_key"] == "sk-aaaaaaaaaaaaaaaaaaaa"

    # 示例占位站：编辑时必须填令牌，提示也要对
    write_config({"站点1": {"base_url": "https://你的中转站地址/v1", "api_key": "在这里填你的key", "proxy": ""}})
    assert rs.list_relays()[0]["placeholder"]
    expect_error(lambda: rs.update_relay("站点1", "站点1", "https://api.b.com/v1", "", ""), "请填写令牌")
    rs.update_relay("站点1", "站点1", "https://api.b.com/v1", "sk-bbbbbbbbbbbbbbbbbbbb", "")
    assert not rs.list_relays()[0]["placeholder"]

    # 只能请求中转站自己的地址
    r = rc.Relay("x", "https://api.a.com/v1", "sk-x")
    assert r.url("https://api.a.com/v1/models") == "https://api.a.com/v1/models"
    expect_error(lambda: r.url("https://evil.example.com/steal"), "只能请求中转站自己的地址")
    assert "Cloudflare" in rc.describe_body("<!DOCTYPE html><html><head><title>Just a moment...</title>", "text/html")


async def t_classify(mock):
    cases = {
        ("gpt-4o-mini", ()): ("text", None),
        ("deepseek-chat", ("openai",)): ("text", None),
        ("claude-sonnet-4-5", ("anthropic", "openai")): ("text", None),
        ("text-embedding-3-small", ()): ("other", None),
        ("text-embedding-3-small", ("openai",)): ("other", None),
        ("gpt-4o-mini-tts", ("openai",)): ("other", None),
        ("bge-reranker-v2-m3", ("jina-rerank",)): ("other", None),
        ("gpt-image-1", ()): ("image", "images"),
        ("gpt-image-1", ("openai",)): ("image", "images"),  # 只有默认的 openai 标注，不能当成对话接口
        ("dall-e-3", ("image-generation", "openai")): ("image", "images"),
        ("gemini-3-pro-image-preview", ()): ("image", "chat"),
        ("gemini-3-pro-image-preview", ("gemini", "openai")): ("image", "gemini"),
        ("gemini-2.5-flash-image", ("image-generation", "openai")): ("image", "images"),
        ("nano-banana-pro", ("openai",)): ("image", "chat"),
        ("doubao-seedream-4-0-250828", ("image-generation", "openai")): ("image", "images-json"),
        ("flux-kontext-pro", ()): ("image", "images"),
        ("sora-2", ()): ("video", "openai"),
        ("sora-2", ("openai-video",)): ("video", "openai"),
        ("sora-2", ("openai",)): ("video", "openai"),
        ("veo3.1-fast", ("openai",)): ("video", "unified"),
        ("kling-v2-1", ("openai",)): ("video", "unified"),  # new-api 给视频任务渠道的默认标注
        ("MiniMax-Hailuo-02", ()): ("video", "unified"),
        ("doubao-seedance-1-0-pro", ()): ("video", "unified"),
        ("wan2.5-t2v-preview", ()): ("video", "unified"),
        ("wan2.5-i2i-preview", ()): ("image", "images"),
        ("gemini-2.5-pro-preview", ("gemini", "openai")): ("text", None),
        ("sora_image", ("openai",)): ("image", "chat"),  # 名字带视频品牌的生图模型
        ("gpt-4o-image", ()): ("image", "chat"),
        ("luma-photon-1", ()): ("image", "images"),
        ("kling-image", ()): ("image", "images"),
        ("veo-3.0-generate-preview", ()): ("video", "unified"),
        ("MiniMax-H3", ("openai",)): ("video", "unified"),  # 不能当成文本：测一下就可能生成一个要扣费的视频
        ("MiniMax-M2.5", ("openai",)): ("text", None),
        ("minimax-m3", ()): ("text", None),
        ("suno_music", ("openai",)): ("other", None),
    }
    for (model, types), (kind, fmt) in cases.items():
        got = rp.classify(model, list(types))
        assert (got["kind"], got["api_format"]) == (kind, fmt), f"{model} {types} -> {got}"

    # 任务插件认领的模型（真实站点返回的 owned_by 和 supported_endpoint_types 就是这样）
    task_cases = {
        ("MiniMax-H3", ("openai",)): ("video", "task"),
        ("MiniMax-Hailuo-2.3", ("openai",)): ("video", "task"),
        ("T2V-01-Director", ("openai",)): ("video", "task"),
        ("kling-v2-master", ("openai",)): ("video", "task"),
        ("doubao-seedance-2-0-260128", ("openai",)): ("video", "task"),
        ("wan2.7-t2v", ("openai",)): ("video", "task"),
        ("wan 3.0-720p-30s", ("openai",)): ("video", "task"),  # 真实站点上的名字，中间有空格
        ("veo-3.1-generate-preview", ("openai",)): ("video", "task"),
        ("grok-imagine-video", ("openai",)): ("video", "task"),
        ("sora-2", ("openai-video",)): ("video", "task"),
        ("some-unknown-video-model", ("openai",)): ("video", "task"),
        ("some-unknown-name", ("openai",)): ("video", "task"),  # 叫什么都不能当成对话模型
        ("some-unknown-name", ("openai-video",)): ("video", "task"),
        ("suno_music", ("openai",)): ("other", None),
        ("incho_music", ("openai",)): ("other", None),
        ("doubao-seedream-5-0-260128", ("openai",)): ("image", "images-json"),
        ("qwen-image-3.0", ("image-generation",)): ("image", "images"),
    }
    for (model, types), (kind, fmt) in task_cases.items():
        got = rp.classify(model, list(types), "task plugin")
        assert (got["kind"], got["api_format"]) == (kind, fmt), f"{model} {types} -> {got}"
        assert fmt != "task" or got["reason"], got
    # 站点没标任务插件时也不能把这些名字当成对话模型，图片模型不能被带偏
    for model, kind in {"wan 3.0-720p-30s": "video", "wan2.2-kf2v-flash": "video", "wan2.6-t2i": "image",
                        "wan2.7-image": "image", "wanx2.1-t2i-turbo": "image"}.items():
        assert rp.classify(model)["kind"] == kind, model

    # 真实探测：模拟站上的这两个模型要被认成任务插件模型
    result = await rp.probe(rc.Relay("task", MOCK_URL, KEY), refresh=True)
    for model_id in ("MiniMax-H3", "wan 3.0-720p-30s"):
        got = next(m for m in result["models"] if m["id"] == model_id)
        assert (got["kind"], got["api_format"]) == ("video", "task"), got
    assert not any("限定了可用模型" in w for w in result["warnings"]), result["warnings"]
    # 限定了模型的令牌：提前告诉用户查不了视频任务，别等扣了费才发现
    limited = await rp.probe(rc.Relay("limited", MOCK_URL, KEY_LIMITED), refresh=True)
    assert limited["ok"] and any("限定了可用模型" in w for w in limited["warnings"]), limited["warnings"]

    # 添加到画布：模型名带空格要原样传过去
    event = manager_api.clean_event({"type": "add-node", "node": "RelayAPI_Video", "relay": "站",
                                     "model": "wan 3.0-720p-30s", "api_format": "task"})
    assert event["model"] == "wan 3.0-720p-30s" and event["api_format"] == "task", event
    expect_error(lambda: manager_api.clean_event({"type": "add-node", "node": "RelayAPI_Video", "relay": "站",
                                                  "model": "m", "api_format": "no-such"}), "节点信息不完整")


# ---------- HTTP 接口 ----------

async def t_api_comfy_mode(mock):
    """ComfyUI 模式：只挂路由，不要求令牌。"""
    write_config({})
    app = aiohttp.web.Application()
    routes = aiohttp.web.RouteTableDef()
    manager_api.add_routes(routes)
    app.add_routes(routes)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 18943)
    await site.start()
    base = "http://127.0.0.1:18943/relay_api"
    try:
        async with aiohttp.ClientSession() as s:
            async def call(method, path, body=None, status=200):
                async with s.request(method, f"{base}/{path}", json=body) as r:
                    data = await r.json()
                    assert r.status == status, (path, r.status, data)
                    return data

            state = await call("GET", "state")
            assert state["relays"] == [] and state["config_path"] == rc.config_path()
            parsed = await call("POST", "parse", {"text": f"地址 {MOCK_URL} 令牌 {KEY}abcdefghij"})
            assert parsed["base_url"] == MOCK_URL

            # 表单测试：未保存的配置
            mock.api_auth.clear()
            res = await call("POST", "test", {"name": "", "base_url": MOCK_URL, "api_key": KEY, "proxy": ""})
            assert res["ok"] and res["software"] == {"name": "New API", "site_name": "某某中转", "version": "v0.9.9"}, \
                res.get("software")
            assert mock.api_auth and all(a is None for a in mock.api_auth), "请求 /api/status、/api/pricing 不能带令牌"
            kinds = {m["id"]: (m["kind"], m["api_format"]) for m in res["models"]}
            assert kinds["gemini-3-pro-image-preview"] == ("image", "gemini"), kinds
            assert kinds["gpt-image-1"] == ("image", "images") and kinds["gpt-4o-mini"] == ("text", None)
            assert kinds["sora-2"] == ("video", "openai") and kinds["kling-v2-1"] == ("video", "unified"), kinds
            assert kinds["doubao-seedream-4-0"] == ("image", "images-json"), kinds
            bad = await call("POST", "test", {"base_url": MOCK_URL, "api_key": "sk-wrong"})
            assert not bad["ok"] and "401" in bad["error"]

            created = await call("POST", "relays", {"name": "模拟站", "base_url": MOCK_URL, "api_key": KEY, "proxy": ""})
            assert created["relay"]["key_masked"] and KEY not in json.dumps(created)
            await call("POST", "relays", {"name": "模拟站", "base_url": MOCK_URL, "api_key": KEY}, status=400)
            state = await call("GET", "state")
            assert KEY not in json.dumps(state), "列表里不能出现完整令牌"

            models = await call("GET", f"models/{aiohttp.helpers.quote('模拟站', safe='')}")
            assert models["counts"]["image"] >= 2 and models["counts"]["video"] >= 1, models["counts"]

            # 编辑时令牌留空：同地址可以沿用；换地址必须重填
            ok = await call("POST", "test", {"base_url": MOCK_URL + "/", "api_key": "", "keep_key_of": "模拟站"})
            assert ok["ok"]
            evil = await call("POST", "test", {"base_url": "https://evil.example.com/v1", "api_key": "", "keep_key_of": "模拟站"},
                              status=400)
            assert "重新填写令牌" in evil["error"]
            evil = await call("POST", "test", {"base_url": MOCK_URL, "api_key": "", "keep_key_of": "模拟站",
                                               "proxy": "http://evil.example.com:8080"}, status=400)
            assert "重新填写令牌" in evil["error"]
            # 防 DNS 重绑定：Host 是域名就拒绝；不是 JSON 的请求也拒绝
            async with s.get(f"{base}/state", headers={"Host": "evil.example.com:18943"}) as r:
                assert r.status == 403
            async with s.get(f"{base}/manager/", headers={"Host": "evil.example.com"}) as r:
                assert r.status == 403
            async with s.post(f"{base}/relays", data="name=x&base_url=https://x.com&api_key=sk-x",
                              headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
                assert r.status == 400 and "JSON" in (await r.json())["error"]
            await call("PUT", f"relays/{aiohttp.helpers.quote('模拟站', safe='')}",
                       {"name": "模拟站", "base_url": "https://evil.example.com/v1", "api_key": ""}, status=400)

            reply = await call("POST", "chat_test", {"name": "模拟站", "model": "mock-chat"})
            assert reply["reply"].startswith("echo:这是连通性测试"), reply

            renamed = await call("PUT", f"relays/{aiohttp.helpers.quote('模拟站', safe='')}",
                                 {"name": "模拟站2", "base_url": MOCK_URL, "api_key": "", "proxy": ""})
            assert renamed["relay"]["name"] == "模拟站2"
            await call("DELETE", f"relays/{aiohttp.helpers.quote('模拟站2', safe='')}")
            assert (await call("GET", "state"))["relays"] == []

            # 页面和静态文件：只放行页面自己的几个文件
            async with s.get(f"{base}/manager") as r:
                assert r.status == 200 and "中转站管理器" in await r.text()  # 自动跳转到 /manager/
            for name in ("app.js", "style.css", "icon.svg", "icon.ico"):
                async with s.get(f"{base}/manager/{name}") as r:
                    assert r.status == 200, name
            for bad_path in ("..%2Frelay_store.py", "relays.json", ".hidden.js", "C:foo.js", "C:%5CWindows%5Cwin.ini"):
                async with s.get(f"{base}/manager/{bad_path}") as r:
                    assert r.status == 404, bad_path
    finally:
        await runner.cleanup()


async def t_api_standalone_security(mock):
    """独立模式：必须带令牌，Host 必须是本机。"""
    write_config({})
    server = manager_app.ManagerServer(port=0)
    await server.start()
    base = f"http://127.0.0.1:{server.port}/relay_api"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/state") as r:
                assert r.status == 403, "没带令牌应该被拒绝"
            async with s.get(f"{base}/state", headers={"X-Relay-Token": "wrong"}) as r:
                assert r.status == 403
            async with s.get(f"{base}/state", headers={"X-Relay-Token": server.token}) as r:
                assert r.status == 200
            async with s.get(f"{base}/state", headers={"X-Relay-Token": server.token, "Host": "evil.example.com"}) as r:
                assert r.status == 403, "Host 不是本机应该被拒绝（防 DNS 重绑定）"
            async with s.get(f"{base}/state?token=%E4%B8%AD%C3%A9") as r:
                assert r.status == 403, "非 ASCII 令牌应该是 403 而不是 500"
            async with s.get(f"{base}/manager/?token={server.token}") as r:
                assert r.status == 200 and "中转站管理器" in await r.text()
            # 再次双击快捷方式：hello 确认身份，代码没变就约好会有新窗口，期间不退出
            server.ever_connected, server.last_disconnect = True, 0.0
            async with s.get(f"{base}/hello", params={"code": "other"}, headers={"X-Relay-Token": server.token}) as r:
                assert (await r.json())["code"] == manager_app.CODE_STAMP
            assert server.should_exit(0.0), "代码不一样的进程不会接着用这个后台，不用等它的窗口"
            async with s.get(f"{base}/hello", params={"code": manager_app.CODE_STAMP},
                             headers={"X-Relay-Token": server.token}) as r:
                assert (await r.json()) == {"app": "relay-manager", "pid": os.getpid(), "code": manager_app.CODE_STAMP}
            assert not server.should_exit(0.0), "约好新窗口后不能退出"
            server.expect_until = 0.0
            assert server.should_exit(0.0)
            server.ever_connected, server.last_disconnect = False, time.monotonic()  # 恢复，继续下面的检查
            # 窗口用 WebSocket 保活：连着时计数为 1，断开后归零，之后才允许退出
            started = asyncio.get_running_loop().time()
            assert not server.should_exit(started)
            async with s.ws_connect(f"ws://127.0.0.1:{server.port}/relay_api/live?token={server.token}"):
                await asyncio.sleep(0.1)
                assert server.connections == 1 and server.ever_connected
            await asyncio.sleep(0.1)
            assert server.connections == 0
            try:
                async with s.ws_connect(f"ws://127.0.0.1:{server.port}/relay_api/live"):
                    raise AssertionError("没带令牌的 WebSocket 应该被拒绝")
            except aiohttp.WSServerHandshakeError as e:
                assert e.status == 403
    finally:
        await server.stop()


async def t_reopen_after_update(mock):
    """插件更新后再双击快捷方式：不能接到还在跑旧代码的后台上，否则重新打开看到的还是旧结果。"""
    write_config({})
    instance_file = manager_app.INSTANCE_FILE
    folder = tempfile.mkdtemp(prefix="relay_instance_")
    manager_app.INSTANCE_FILE = os.path.join(folder, "instance.json")
    server = manager_app.ManagerServer(port=0)
    await server.start()
    stopped = False
    try:
        manager_app._write_instance(server)
        # 代码没变：接着用，只是再开一个窗口
        assert await manager_app._running_instance() == server.url and not server.quitting
        # 只有带令牌才能让它退出
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{server.port}/relay_api/quit") as r:
                assert r.status == 403 and not server.quitting
        # 代码变了：让旧后台退出，等它让出端口，不把它的地址交给新窗口
        server.code = "before-update"
        task = asyncio.ensure_future(manager_app._running_instance())
        while not server.quitting:
            await asyncio.sleep(0.02)
        assert server.should_exit(0.0)
        await server.stop()
        stopped = True
        assert await task == manager_app._RETIRED
    finally:
        if not stopped:
            await server.stop()

    # 更早的版本：hello 不报代码指纹，也没有退出接口。不接着用它，也不用等它
    async def hello(request):
        return aiohttp.web.json_response({"app": "relay-manager", "pid": os.getpid()})

    app = aiohttp.web.Application()
    app.router.add_get("/relay_api/hello", hello)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        with open(manager_app.INSTANCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "token": "t"}, f)
        start = time.monotonic()
        assert await manager_app._running_instance() == manager_app._RETIRED
        assert time.monotonic() - start < 2
    finally:
        await runner.cleanup()
        manager_app.INSTANCE_FILE = instance_file
        shutil.rmtree(folder, ignore_errors=True)


async def t_bridge(mock):
    """独立管理器 → 正在运行的 ComfyUI：找到端口、转发「刷新下拉框」「添加节点」通知。"""
    write_config({})
    events, pages = [], {"count": 1}

    def send(event):
        events.append(event)
        return pages["count"]  # 收到通知的 ComfyUI 页面数量

    comfy = aiohttp.web.Application()
    routes = aiohttp.web.RouteTableDef()
    manager_api.add_routes(routes)
    manager_api.add_comfy_routes(routes, send)
    comfy.add_routes(routes)
    runner = aiohttp.web.AppRunner(comfy)
    await runner.setup()
    await aiohttp.web.TCPSite(runner, "127.0.0.1", 18944).start()
    server = manager_app.ManagerServer(port=0, comfy_ports=[18945, 18944])
    await server.start()
    base = f"http://127.0.0.1:{server.port}/relay_api"
    headers = {"X-Relay-Token": server.token}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/comfy/status", headers=headers) as r:
                assert (await r.json()) == {"running": True, "port": 18944}
            node = {"type": "add-node", "node": "RelayAPI_Image", "relay": "主站", "model": "gpt-image-1",
                    "api_format": "images", "extra": "会被丢掉"}
            async with s.post(f"{base}/comfy/notify", json=node, headers=headers) as r:
                assert r.status == 200, await r.text()
            async with s.post(f"{base}/comfy/notify", json={"type": "relays-changed"}, headers=headers) as r:
                assert r.status == 200
            assert events == [{k: v for k, v in node.items() if k != "extra"}, {"type": "relays-changed"}], events
            for bad in ({"type": "run-code"}, {"type": "add-node", "node": "KSampler", "relay": "x", "model": "y"},
                        {"type": "add-node", "node": "RelayAPI_Chat", "relay": "x", "model": "y", "api_format": "evil"}):
                async with s.post(f"{base}/comfy/notify", json=bad, headers=headers) as r:
                    assert r.status == 400, bad
            assert len(events) == 2
            # ComfyUI 开着但没有打开的页面：加节点要报错，不能谎报成功
            pages["count"] = 0
            async with s.post(f"{base}/comfy/notify", json=node, headers=headers) as r:
                assert r.status == 400 and "没有打开的 ComfyUI 界面" in (await r.json())["error"]
        await runner.cleanup()
        server.comfy.checked = -999  # 让它重新检测
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/comfy/status", headers=headers) as r:
                assert (await r.json()) == {"running": False, "port": None}
            async with s.post(f"{base}/comfy/notify", json={"type": "relays-changed"}, headers=headers) as r:
                assert r.status == 400 and "没有检测到正在运行的 ComfyUI" in (await r.json())["error"]
    finally:
        await server.stop()
        await runner.cleanup()


TESTS = [t_store_crud, t_parse, t_store_edge, t_classify, t_api_comfy_mode, t_api_standalone_security,
         t_reopen_after_update, t_bridge]


async def main() -> int:
    folder = tempfile.mkdtemp(prefix="relay_mgr_test_")
    os.environ[rc.CONFIG_ENV] = os.path.join(folder, "relays.json")
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
        shutil.rmtree(folder, ignore_errors=True)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} 通过")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
