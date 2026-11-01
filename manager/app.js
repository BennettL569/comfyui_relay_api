// 中转站管理器前端：ComfyUI 侧边栏里（?embedded=1）和独立窗口里（?token=...）共用这一份。
const params = new URLSearchParams(location.search);
const TOKEN = params.get("token") || "";
const EMBEDDED = params.get("embedded") === "1" && window.parent !== window;
const root = document.documentElement;
if (EMBEDDED) root.classList.add("embedded");

const KIND_LABEL = { text: "文本", image: "图片", video: "视频", other: "其他" };
const FORMAT_LABEL = {
  images: "OpenAI 图像接口",
  "images-json": "图像接口·JSON 参考图",
  chat: "对话接口",
  gemini: "Gemini 原生",
  unified: "统一视频接口",
  openai: "Sora 格式",
  custom: "自定义路径",
  task: "任务插件接口",
};
const NODE_FOR_KIND = { text: "RelayAPI_Chat", image: "RelayAPI_Image", video: "RelayAPI_Video" };
const MAX_ROWS = 300;
const TEXT_INPUT = { spellcheck: false, autocomplete: "off", autocapitalize: "off" }; // 别让浏览器的拼写检查把令牌发出去

const state = {
  configPath: "",
  loadError: "",
  relays: [],
  placeholders: [],
  selected: null,
  view: "detail", // detail | form
  form: null,
  probes: {}, // 中转站名 -> 探测结果
  probeGen: {}, // 中转站名 -> 第几次探测（配置改过后，旧的探测结果作废）
  probing: new Set(),
  filter: "all",
  search: "",
  replies: {}, // `${中转站}|${模型}` -> {busy, ok, text}
  comfy: { running: false, port: null }, // 独立窗口模式下，本机正在运行的 ComfyUI
  backendDown: false,
};

// 能不能把节点放到 ComfyUI 画布上：侧边栏里直接放；独立窗口里要检测到正在运行的 ComfyUI
const canvasAvailable = () => EMBEDDED || state.comfy.running;

// ---------- 工具 ----------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null) continue;
    if (value === false) {
      if (key in node) node[key] = false; // 例如 spellcheck=false、disabled=false
      continue;
    }
    if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (TOKEN) headers["X-Relay-Token"] = TOKEN;
  let res;
  try {
    res = await fetch(`../${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new Error(TOKEN ? "连不上管理器后台，请重新打开管理器" : "连不上 ComfyUI，请确认 ComfyUI 还在运行");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!res.ok || data?.error) throw new Error(data?.error || `请求失败（HTTP ${res.status}）`);
  return data;
}

function toast(text, kind = "") {
  const item = el("div", { class: `toast ${kind}` }, text);
  document.getElementById("toasts").append(item);
  setTimeout(() => item.remove(), kind === "err" ? 7000 : 3500);
}

// 确认框自己画，不用原生 confirm()：Comfy Desktop（Electron）在 Windows 上弹过原生对话框后，
// 输入框常常打不了字，弹框期间整个 ComfyUI 页面也会卡住
function askConfirm(message, okText = "确定", danger = false) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const behind = [...document.body.children].filter((node) => node.id !== "toasts");
    const finish = (answer) => {
      mask.remove();
      behind.forEach((node) => { node.inert = false; });
      previous?.focus?.();
      resolve(answer);
    };
    const cancel = el("button", { class: "btn", onclick: () => finish(false) }, "取消");
    const mask = el("div", {
      class: "modal-mask",
      onkeydown: (e) => {
        if (e.key === "Escape") finish(false);
      },
    }, el("div", { class: "card modal", role: "alertdialog", "aria-modal": "true" },
      el("div", { class: "text" }, message),
      el("div", { class: "actions" },
        cancel,
        el("button", { class: `btn ${danger ? "danger" : "primary"}`, onclick: () => finish(true) }, okText))));
    behind.forEach((node) => { node.inert = true; });
    document.body.append(mask);
    cancel.focus(); // 默认停在不会丢东西的那个按钮上
  });
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url || "";
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = el("textarea", { value: text, style: "position:fixed;opacity:0" });
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(`已复制：${text}`, "ok");
}

function postToComfy(message) {
  if (EMBEDDED) window.parent.postMessage({ source: "relay-api-manager", ...message }, location.origin);
}

function notifyChanged() {
  if (EMBEDDED) postToComfy({ type: "relays-changed" });
  else if (TOKEN) api("comfy/notify", { method: "POST", body: { type: "relays-changed" } }).catch(() => {});
}

async function checkComfy() {
  let status = { running: false, port: null };
  try {
    status = await api("comfy/status");
  } catch {
    /* 后台已退出 */
  }
  if (status.running !== state.comfy.running || status.port !== state.comfy.port) {
    state.comfy = status;
    render();
  }
}

const narrowQuery = window.matchMedia("(max-width: 720px)");
const applyNarrow = () => root.classList.toggle("narrow", narrowQuery.matches);
narrowQuery.addEventListener("change", applyNarrow);
applyNarrow();

// ---------- 数据 ----------

function invalidate(name) {
  state.probeGen[name] = (state.probeGen[name] || 0) + 1;
  state.probing.delete(name);
  delete state.probes[name];
}

async function loadState({ quiet = false } = {}) {
  let data;
  try {
    data = await api("state");
  } catch (e) {
    if (quiet) return;
    state.loadError = e.message;
    render();
    return;
  }
  // 定时刷新时没变化就不重画，免得打断正在进行的点击
  if (quiet && !state.loadError && JSON.stringify([data.relays, data.placeholders])
    === JSON.stringify([state.relays, state.placeholders])) return;
  // 别处（侧边栏 / 独立窗口 / 手动编辑）改过的中转站，旧的测试结果作废
  const before = new Map(state.relays.map((r) => [r.name, r]));
  for (const relay of data.relays) {
    const old = before.get(relay.name);
    if (!old || old.base_url !== relay.base_url || old.key_masked !== relay.key_masked || old.proxy !== relay.proxy) {
      if (old) invalidate(relay.name);
    }
    before.delete(relay.name);
  }
  for (const name of before.keys()) invalidate(name);

  state.loadError = "";
  state.configPath = data.config_path;
  state.relays = data.relays;
  state.placeholders = data.placeholders || [];
  if (state.selected && !state.relays.some((r) => r.name === state.selected)) state.selected = null;
  if (!state.selected && state.relays.length) {
    state.selected = (state.relays.find((r) => r.configured) || state.relays[0]).name;
  }
  render();
  const current = state.relays.find((r) => r.name === state.selected);
  if (current?.configured && !state.probes[current.name]) probe(current.name);
}

async function probe(name, force = false) {
  if (state.probing.has(name) && !force) return;
  const gen = (state.probeGen[name] || 0) + 1;
  state.probeGen[name] = gen;
  state.probing.add(name);
  render();
  let result;
  try {
    result = await api(`models/${encodeURIComponent(name)}${force ? "?refresh=1" : ""}`);
  } catch (e) {
    result = { ok: false, error: e.message };
  }
  if (state.probeGen[name] !== gen) return; // 期间配置改过或又测了一次，这个结果作废
  state.probes[name] = result;
  state.probing.delete(name);
  render();
}

async function leaveForm() {
  if (state.view === "form" && state.form?.dirty && !(await askConfirm("放弃没保存的修改吗？", "放弃", true))) return false;
  state.form = null;
  state.view = "detail";
  return true;
}

async function select(name) {
  if (!(await leaveForm())) return;
  state.selected = name;
  render();
  if (root.classList.contains("narrow")) {
    document.getElementById("detail").scrollIntoView({ block: "start", behavior: "smooth" });
  }
  const relay = state.relays.find((r) => r.name === name);
  if (relay?.configured && !state.probes[name]) probe(name);
}

// ---------- 渲染 ----------

function statusChip(relay, result) {
  if (state.probing.has(relay.name)) return el("span", { class: "chip" }, el("span", { class: "spinner" }), "测试中");
  if (!relay.configured) return el("span", { class: "chip warn" }, "未配置");
  if (result?.ok && !result.models.length) return el("span", { class: "chip warn" }, "● 没有模型");
  if (result?.ok) return el("span", { class: "chip ok" }, "● 正常");
  if (result) return el("span", { class: "chip err" }, "● 出错");
  return el("span", { class: "chip" }, "未测试");
}

function render() {
  // 重新渲染会替换搜索框，记住它的焦点和光标，不然用户打字打到一半会丢
  const active = document.activeElement;
  const searchFocus = active?.dataset?.role === "model-search" ? [active.selectionStart, active.selectionEnd] : null;

  document.getElementById("cfgPath").textContent = state.configPath ? `配置文件：${state.configPath}` : "";
  renderBanner();
  renderList();
  const detail = document.getElementById("detail");
  if (state.view === "form" && state.form) {
    if (!detail.contains(state.form.root)) detail.replaceChildren(state.form.root);
  } else {
    detail.replaceChildren();
    renderDetail(detail);
  }

  if (searchFocus) {
    const input = document.querySelector('[data-role="model-search"]');
    if (input) {
      input.focus();
      input.setSelectionRange(...searchFocus);
    }
  }
}

function renderBanner() {
  const box = document.getElementById("banner");
  box.replaceChildren();
  if (state.backendDown) {
    box.append(el("div", { class: "banner" }, el("span", {}, "管理器后台已经退出了，请重新打开管理器。")));
    return;
  }
  if (!state.placeholders.length) return;
  box.append(
    el("div", { class: "banner" },
      el("span", {}, `配置里有 ${state.placeholders.length} 个还没填写的示例中转站（${state.placeholders.join("、")}）。`),
      el("button", { class: "btn small", onclick: removePlaceholders }, "删掉示例")),
  );
}

function renderList() {
  const box = document.getElementById("relayList");
  box.replaceChildren();
  if (state.loadError) {
    box.append(
      el("div", { class: "card result err" },
        el("div", { class: "title" }, "读取配置失败"),
        el("div", {}, state.loadError),
        el("div", { class: "note" }, "如果是手动改坏了 relays.json，可以改正后点重新加载；上一次保存的版本在同目录的 relays.json.bak。"),
        el("div", { class: "actions", style: "margin-top:8px" }, el("button", { class: "btn small", onclick: loadState }, "重新加载"))),
    );
    return;
  }
  if (!state.relays.length) {
    box.append(
      el("div", { class: "card empty" },
        el("div", { class: "big" }, "还没有中转站"),
        el("div", { class: "muted" }, "点「添加中转站」，把站点给你的地址和令牌整段粘贴进来就行。")),
    );
    return;
  }
  const configured = state.relays.filter((r) => r.configured);
  if (configured.length > 1) {
    box.append(
      el("div", { class: "actions", style: "margin-bottom:10px" },
        el("button", { class: "btn small", onclick: () => configured.forEach((r) => probe(r.name, true)) }, "全部测试")),
    );
  }
  for (const relay of state.relays) {
    const result = state.probes[relay.name];
    let counts = relay.configured ? "点击查看并测试连接" : "还没填写地址和令牌";
    if (result?.ok) counts = `文本 ${result.counts.text} · 图片 ${result.counts.image} · 视频 ${result.counts.video}`;
    else if (result) counts = "连接失败，点开看原因";
    const active = state.view === "detail" && state.selected === relay.name;
    box.append(
      el("div", { class: `card relay${active ? " active" : ""}`, onclick: () => select(relay.name) },
        el("div", { class: "row" }, el("div", { class: "name", title: relay.name }, relay.name), statusChip(relay, result)),
        el("div", { class: "host", title: relay.base_url }, relay.url_filled ? hostOf(relay.base_url) : "（未填写接口地址）"),
        el("div", { class: "counts" }, counts)),
    );
  }
}

function renderDetail(box) {
  if (state.loadError) return;
  const relay = state.relays.find((r) => r.name === state.selected);
  if (!relay) {
    box.append(el("div", { class: "card empty muted" }, state.relays.length ? "在左边选一个中转站" : "添加中转站后，这里会显示它的模型"));
    return;
  }
  const result = state.probes[relay.name];
  const busy = state.probing.has(relay.name);
  const info = [
    el("dt", {}, "接口地址"), el("dd", { class: "mono" }, relay.url_filled ? relay.base_url : "未填写"),
    el("dt", {}, "令牌"), el("dd", { class: "mono" }, relay.key_masked || (relay.api_key_env ? `环境变量 ${relay.api_key_env}` : "未填写")),
    el("dt", {}, "代理"), el("dd", {}, relay.proxy || "不使用"),
  ];
  const software = result?.software;
  if (software?.site_name && software.site_name !== software.name) {
    info.push(el("dt", {}, "站点名称"), el("dd", {}, software.site_name));
  }
  if (software?.name) {
    info.push(el("dt", {}, "站点程序"), el("dd", {}, `${software.name} ${software.version || ""}`.trim()));
  }
  if (result?.ok) info.push(el("dt", {}, "响应耗时"), el("dd", {}, `${result.latency_ms} ms`));

  box.append(
    el("div", { class: "card" },
      el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px" },
        el("h2", { style: "min-width:0;overflow-wrap:anywhere" }, relay.name), statusChip(relay, result)),
      el("dl", { class: "info-grid" }, info),
      el("div", { class: "actions" },
        el("button", { class: "btn primary", disabled: busy || !relay.configured, onclick: () => probe(relay.name, true) },
          busy ? [el("span", { class: "spinner" }), " 测试中"] : "测试连接"),
        el("button", { class: "btn", onclick: () => openForm("edit", relay) }, relay.configured ? "编辑" : "填写"),
        el("button", { class: "btn danger", onclick: () => removeRelay(relay) }, "删除")),
      result && !result.ok ? el("div", { class: "result err" }, el("div", { class: "title" }, "连接失败"), result.error) : null,
      result?.ok && result.warnings?.length ? el("div", { class: "note" }, result.warnings.join("；")) : null,
      result?.ok ? renderModels(relay, result) : null,
      EMBEDDED ? null : el("div", { class: "note" }, state.comfy.running
        ? `已连接到正在运行的 ComfyUI（端口 ${state.comfy.port}），这里的修改会自动同步到节点的「中转站」下拉框。`
        : "没检测到正在运行的 ComfyUI。启动 ComfyUI 后（Comfy Desktop 要用本地实例，Comfy Cloud 装不了插件），节点的「中转站」下拉框里就能选到这里配置的站。")),
  );
}

function renderModels(relay, result) {
  if (!result.models.length) {
    return el("div", { class: "result err" }, el("div", { class: "title" }, "站点没有返回任何模型"),
      "可能是这个令牌没有分组权限，或者站点还没上架模型，去站点后台看看令牌设置。");
  }
  const list = el("div", { class: "models", style: "margin-top:10px" });
  const refresh = () => {
    const query = state.search.trim().toLowerCase();
    const items = result.models.filter(
      (m) => (state.filter === "all" || m.kind === state.filter) && (!query || m.id.toLowerCase().includes(query)),
    );
    const rows = items.slice(0, MAX_ROWS).map((m) => modelRow(relay, m));
    if (!rows.length) rows.push(el("div", { class: "empty muted" }, "没有匹配的模型"));
    if (items.length > MAX_ROWS) rows.push(el("div", { class: "empty muted" }, `还有 ${items.length - MAX_ROWS} 个，用搜索缩小范围`));
    list.replaceChildren(...rows);
  };
  const search = el("input", {
    ...TEXT_INPUT,
    "data-role": "model-search",
    placeholder: "搜索模型名，例如 banana、sora、gpt",
    value: state.search,
    oninput: (e) => {
      state.search = e.target.value;
      refresh();
    },
  });
  const filters = el("div", { class: "filters" },
    ["all", "text", "image", "video", "other"].map((kind) =>
      el("span", {
        class: `chip${state.filter === kind ? " active" : ""}`,
        onclick: () => {
          state.filter = kind;
          render();
        },
      }, kind === "all" ? `全部 ${result.models.length}` : `${KIND_LABEL[kind]} ${result.counts[kind] ?? 0}`)),
  );
  refresh();
  return el("div", {},
    el("h3", {}, "模型"),
    el("div", { class: "toolbar" }, search),
    filters,
    list,
    canvasAvailable()
      ? el("div", { class: "note" }, "「添加到画布」会在 ComfyUI 画布中间放一个配好的节点，接口格式按推荐的来，不对可以在节点上再改。")
      : null,
  );
}

function modelRow(relay, model) {
  const key = `${relay.name}|${model.id}`;
  const reply = state.replies[key];
  const actions = el("div", { class: "actions" });
  if (model.kind === "text") {
    actions.append(el("button", { class: "btn small", disabled: reply?.busy, onclick: () => chatTest(relay, model) },
      reply?.busy ? "测试中…" : "测一下"));
  }
  if (canvasAvailable() && NODE_FOR_KIND[model.kind]) {
    actions.append(el("button", { class: "btn small primary", onclick: () => addToCanvas(relay, model) }, "添加到画布"));
  }
  actions.append(el("button", { class: "btn small", onclick: () => copyText(model.id) }, "复制"));
  return el("div", { class: "model" },
    el("div", { class: "id", title: model.id }, model.id),
    el("div", { class: "meta" },
      el("span", { class: `chip kind-${model.kind}` }, KIND_LABEL[model.kind] || model.kind),
      model.api_format
        ? el("span", {
          class: "chip",
          title: [model.reason, model.endpoint_types?.length ? `站点标注：${model.endpoint_types.join("、")}` : ""]
            .filter(Boolean).join("\n"),
        }, FORMAT_LABEL[model.api_format] || model.api_format)
        : null),
    actions,
    reply && !reply.busy ? el("div", { class: "reply" }, `${reply.ok ? "✓" : "✗"} ${reply.text}`) : null,
  );
}

// ---------- 操作 ----------

async function chatTest(relay, model) {
  const key = `${relay.name}|${model.id}`;
  state.replies[key] = { busy: true };
  render();
  try {
    const res = await api("chat_test", { method: "POST", body: { name: relay.name, model: model.id } });
    state.replies[key] = { ok: true, text: `${res.reply || "（空回复）"}（${res.latency_ms} ms）` };
  } catch (e) {
    state.replies[key] = { ok: false, text: e.message };
  }
  render();
}

async function addToCanvas(relay, model) {
  const message = {
    type: "add-node",
    node: NODE_FOR_KIND[model.kind],
    relay: relay.name,
    model: model.id,
    api_format: model.api_format || null,
  };
  if (EMBEDDED) {
    postToComfy(message);
    return;
  }
  try {
    await api("comfy/notify", { method: "POST", body: message });
    toast(`已发送到 ComfyUI 画布：${model.id}`, "ok");
  } catch (e) {
    toast(e.message, "err");
    checkComfy();
  }
}

async function removeRelay(relay) {
  if (!(await askConfirm(`确定删除中转站「${relay.name}」吗？用到它的工作流需要重新选择中转站。`, "删除", true))) return;
  try {
    await api(`relays/${encodeURIComponent(relay.name)}`, { method: "DELETE" });
    toast(`已删除「${relay.name}」`, "ok");
    invalidate(relay.name);
    state.selected = null;
    notifyChanged();
    await loadState();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function removePlaceholders() {
  try {
    const res = await api("placeholders/remove", { method: "POST" });
    toast(res.removed.length ? `已删除：${res.removed.join("、")}` : "没有需要删除的示例", "ok");
    notifyChanged();
    await loadState();
  } catch (e) {
    toast(e.message, "err");
  }
}

// ---------- 添加 / 编辑表单 ----------

async function openForm(mode, relay = null) {
  if (state.view === "form" && !(await leaveForm())) return;
  const hasKey = Boolean(relay && (relay.key_masked || relay.api_key_env));
  const values = {
    name: relay?.name || "",
    base_url: relay?.url_filled ? relay.base_url : "",
    api_key: "",
    proxy: relay?.proxy || "",
  };
  // 还没填过令牌的（比如示例占位）也用「添加」的样子：有粘贴框，令牌必填
  const form = { mode, original: relay?.name || null, values, hasKey, autoName: !relay, busy: false, dirty: false, refs: {} };
  state.form = form;
  state.view = "form";
  form.root = buildForm(form, relay);
  render();
  (hasKey ? form.refs.name : form.refs.paste).focus();
}

function buildForm(form, relay) {
  const v = form.values;
  const refs = form.refs;
  const field = (label, control, hint) =>
    el("div", { class: "field" }, el("label", {}, label), control, hint ? el("div", { class: "hint" }, hint) : null);
  const input = (key, props = {}) =>
    (refs[key] = el("input", {
      ...TEXT_INPUT,
      value: v[key],
      oninput: (e) => {
        v[key] = e.target.value;
        form.dirty = true;
        if (key === "name") form.autoName = false;
      },
      ...props,
    }));

  let timer = null;
  refs.paste = el("textarea", {
    ...TEXT_INPUT,
    placeholder: "把站点给你的地址和令牌整段粘贴到这里",
    oninput: (e) => {
      form.dirty = true;
      clearTimeout(timer);
      const text = e.target.value;
      timer = setTimeout(() => quickParse(form, text), 250);
    },
  });

  refs.key = input("api_key", {
    type: "password",
    placeholder: form.hasKey ? `留空则沿用原令牌（${relay.key_masked || `环境变量 ${relay.api_key_env}`}）` : "sk-...",
  });
  const toggle = el("button", {
    class: "btn",
    type: "button",
    onclick: () => {
      refs.key.type = refs.key.type === "password" ? "text" : "password";
      toggle.textContent = refs.key.type === "password" ? "显示" : "隐藏";
    },
  }, "显示");

  refs.status = el("div");
  refs.test = el("button", { class: "btn", onclick: () => testForm(form) }, "测试连接");
  refs.save = el("button", { class: "btn primary", onclick: () => saveForm(form) }, form.mode === "add" ? "保存" : "保存修改");

  return el("div", { class: "card" },
    el("h2", {}, form.mode === "add" ? "添加中转站" : `编辑「${relay.name}」`),
    form.hasKey ? null : field("快速粘贴", refs.paste, "例如：接口地址 https://api.xxx.com/v1  令牌 sk-xxxx，会自动识别并填好下面的格子"),
    field("名称", input("name", { placeholder: "随便起，例如 closeai、备用站" }), "会显示在节点的「中转站」下拉框里"),
    field("接口地址", input("base_url", { placeholder: "https://api.xxx.com/v1" }), "带不带 /v1 都行，误粘了 /chat/completions 也会自动去掉"),
    field("令牌（API Key）", el("div", { class: "key-row" }, refs.key, toggle)),
    field("代理（可选）", input("proxy", { placeholder: "需要代理才能访问时填，例如 http://127.0.0.1:7890" })),
    el("div", { class: "actions" },
      refs.test,
      refs.save,
      el("button", {
        class: "btn",
        onclick: async () => {
          if (await leaveForm()) render();
        },
      }, "取消")),
    refs.status,
  );
}

async function quickParse(form, text) {
  if (!text.trim()) return;
  try {
    const parsed = await api("parse", { method: "POST", body: { text } });
    const v = form.values;
    const found = [];
    if (parsed.base_url) {
      v.base_url = parsed.base_url;
      found.push("接口地址");
    }
    if (parsed.api_key) {
      v.api_key = parsed.api_key;
      found.push("令牌");
    }
    if (parsed.proxy) {
      v.proxy = parsed.proxy;
      found.push("代理");
    }
    if (parsed.name && (form.autoName || !v.name)) {
      v.name = parsed.name;
      form.autoName = true;
    }
    for (const key of ["name", "base_url", "proxy"]) form.refs[key].value = v[key];
    form.refs.key.value = v.api_key;
    setFormStatus(form, found.length
      ? { ok: true, title: `已识别：${found.join("、")}`, detail: "检查一下没问题就可以测试连接或直接保存" }
      : { ok: false, title: "没识别出地址或令牌", detail: "可以直接在下面的格子里手动填写" });
  } catch (e) {
    setFormStatus(form, { ok: false, title: "识别失败", detail: e.message });
  }
}

function setFormStatus(form, result) {
  form.refs.status.replaceChildren(
    result ? el("div", { class: `result ${result.ok ? "ok" : "err"}` }, el("div", { class: "title" }, result.title), result.detail || "") : "",
  );
}

function setBusy(form, busy, label) {
  form.busy = busy;
  form.refs.test.disabled = busy;
  form.refs.save.disabled = busy;
  if (busy && label) setFormStatus(form, { ok: true, title: label, detail: "" });
}

async function testForm(form) {
  setBusy(form, true, "正在测试连接…");
  try {
    const res = await api("test", {
      method: "POST",
      body: { ...form.values, keep_key_of: form.mode === "edit" ? form.original : null },
    });
    if (res.ok && !res.models.length) {
      setFormStatus(form, { ok: false, title: "连上了，但站点没有返回任何模型", detail: (res.warnings || []).join("；") });
    } else if (res.ok) {
      const software = res.software?.name ? ` · ${res.software.name} ${res.software.version || ""}` : "";
      const warnings = res.warnings?.length ? `\n${res.warnings.join("；")}` : "";
      setFormStatus(form, {
        ok: true,
        title: `连通，共 ${res.models.length} 个模型`,
        detail: `文本 ${res.counts.text} · 图片 ${res.counts.image} · 视频 ${res.counts.video}${software} · ${res.latency_ms} ms${warnings}`,
      });
    } else {
      setFormStatus(form, { ok: false, title: "连接失败", detail: res.error });
    }
  } catch (e) {
    setFormStatus(form, { ok: false, title: "请检查", detail: e.message }); // 没发出请求，是填写的内容有问题
  } finally {
    setBusy(form, false);
  }
}

async function saveForm(form) {
  const renaming = form.mode === "edit" && form.values.name.trim() !== form.original;
  if (renaming && !(await askConfirm(`改名后，已经用到「${form.original}」的工作流需要重新选择中转站。确定改名吗？`, "改名"))) return;
  setBusy(form, true, "正在保存…");
  const body = { ...form.values };
  try {
    const res = form.mode === "add"
      ? await api("relays", { method: "POST", body })
      : await api(`relays/${encodeURIComponent(form.original)}`, { method: "PUT", body });
    const name = res.relay.name;
    toast(form.mode === "add" ? `已添加「${name}」` : `已保存「${name}」`, "ok");
    if (form.original) invalidate(form.original);
    invalidate(name);
    state.selected = name;
    state.view = "detail";
    state.form = null;
    notifyChanged();
    await loadState();
    probe(name, true);
  } catch (e) {
    setBusy(form, false);
    setFormStatus(form, { ok: false, title: "保存失败", detail: e.message });
  }
}

// ---------- 启动 ----------

let started = null; // 第一次读取配置
if (EMBEDDED) {
  // 浏览器不支持无刷新挪动 iframe 时，ComfyUI 切换侧边栏会让这个页面重新加载（见 web/relay_manager.js）：
  // 那边在页面销毁前用 draft() 取走没保存的表单，重新加载后用 restore() 交回来。
  window.relayManager = {
    draft() {
      const form = state.view === "form" ? state.form : null;
      if (!form?.dirty) return null;
      return { mode: form.mode, original: form.original, values: { ...form.values }, autoName: form.autoName, paste: form.refs.paste.value };
    },
    async restore(draft) {
      await started;
      if (state.view === "form") return; // 已经在填别的了
      const relay = state.relays.find((r) => r.name === draft.original) || null;
      if (draft.original && !relay) return; // 正在编辑的中转站已经被删掉了
      await openForm(draft.mode, relay);
      const form = state.form;
      Object.assign(form.values, draft.values);
      form.autoName = draft.autoName;
      form.dirty = true;
      for (const key of ["name", "base_url", "proxy"]) form.refs[key].value = form.values[key];
      form.refs.key.value = form.values.api_key;
      form.refs.paste.value = draft.paste;
      setFormStatus(form, { ok: true, title: "已恢复刚才没保存的内容", detail: "面板刚才被关掉过，检查一下再保存" });
    },
  };
}

window.addEventListener("message", (event) => {
  if (!EMBEDDED || event.source !== window.parent || event.origin !== location.origin) return;
  const msg = event.data;
  if (!msg || msg.source !== "relay-api-comfy") return;
  if (msg.type === "add-node-result") {
    toast(msg.ok ? `已添加到画布：${msg.title}` : `添加失败：${msg.error}`, msg.ok ? "ok" : "err");
  } else if (msg.type === "reload") {
    loadState();
  }
});

if (TOKEN) {
  // 独立窗口模式：和后台保持一条连接，窗口关掉后后台会自动退出
  let failures = 0;
  const connect = () => {
    const url = new URL(`../live?token=${encodeURIComponent(TOKEN)}`, location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url);
    socket.onopen = () => {
      failures = 0;
      if (state.backendDown) {
        state.backendDown = false;
        render();
      }
    };
    socket.onclose = () => {
      failures += 1;
      if (failures >= 3 && !state.backendDown) {
        state.backendDown = true;
        state.comfy = { running: false, port: null };
        render();
      }
      setTimeout(connect, Math.min(30000, 2000 * failures));
    };
  };
  connect();
  checkComfy();
  setInterval(() => {
    if (state.backendDown) return;
    checkComfy();
    if (state.view !== "form") loadState({ quiet: true }); // ComfyUI 侧边栏里改过的话这里也跟上
  }, 10000);
}

document.getElementById("btnAdd").addEventListener("click", () => openForm("add"));
started = loadState();
