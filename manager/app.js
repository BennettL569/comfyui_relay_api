// 中转站管理器前端：ComfyUI 侧边栏里（?embedded=1）和独立窗口里（?token=...）共用这一份。
const params = new URLSearchParams(location.search);
const TOKEN = params.get("token") || "";
const EMBEDDED = params.get("embedded") === "1" && window.parent !== window;
const root = document.documentElement;
if (EMBEDDED) root.classList.add("embedded");

// 默认英文。侧边栏里跟随 ComfyUI 的语言设置（web/relay_manager.js 传进来），独立窗口用右上角的开关
const LANG_KEY = "relay-manager-lang";
function storedLang() {
  try {
    return localStorage.getItem(LANG_KEY);
  } catch {
    return null;
  }
}
const LANG = (EMBEDDED ? params.get("lang") : storedLang()) === "zh" ? "zh" : "en";
root.lang = LANG;
let catalog = {};
if (LANG === "zh") {
  try {
    catalog = await (await fetch("zh.json")).json();
  } catch {
    /* 翻译读不到就显示英文 */
  }
}

function t(text, values) {
  const template = catalog[text] ?? text;
  return values ? template.replace(/\{(\w+)\}/g, (match, key) => (key in values ? values[key] : match)) : template;
}

const KIND_LABEL = { text: t("Text"), image: t("Image"), video: t("Video"), other: t("Other") };
const FORMAT_LABEL = {
  images: t("OpenAI image API"),
  "images-json": t("Image API with JSON references"),
  chat: t("Chat API"),
  gemini: t("Native Gemini"),
  unified: t("Unified video API"),
  openai: t("Sora format"),
  custom: t("Custom paths"),
  task: t("Task plugin API"),
};
const NODE_FOR_KIND = { text: "RelayAPI_Chat", image: "RelayAPI_Image", video: "RelayAPI_Video" };
const MAX_ROWS = 300;
const TEXT_INPUT = { spellcheck: false, autocomplete: "off", autocapitalize: "off" }; // 别让浏览器的拼写检查把令牌发出去

const state = {
  configPath: "",
  canEdit: true,
  standalone: false,
  panelEdit: false,
  startScript: "",
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
    throw new Error(TOKEN ? t("Cannot reach the manager backend. Please reopen the manager") : t("Cannot reach ComfyUI. Please check it is still running"));
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!res.ok || data?.error) throw new Error(data?.error || t("Request failed (HTTP {status})", { status: res.status }));
  return data;
}

function toast(text, kind = "") {
  const item = el("div", { class: `toast ${kind}` }, text);
  document.getElementById("toasts").append(item);
  setTimeout(() => item.remove(), kind === "err" ? 7000 : 3500);
}

// 确认框自己画，不用原生 confirm()：Comfy Desktop（Electron）在 Windows 上弹过原生对话框后，
// 输入框常常打不了字，弹框期间整个 ComfyUI 页面也会卡住
function askConfirm(message, okText = t("OK"), danger = false) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const behind = [...document.body.children].filter((node) => node.id !== "toasts");
    const finish = (answer) => {
      mask.remove();
      behind.forEach((node) => { node.inert = false; });
      previous?.focus?.();
      resolve(answer);
    };
    const cancel = el("button", { class: "btn", onclick: () => finish(false) }, t("Cancel"));
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
  toast(t("Copied: {text}", { text }), "ok");
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
  if (quiet && !state.loadError && JSON.stringify([data.relays, data.placeholders, data.can_edit, data.panel_edit])
    === JSON.stringify([state.relays, state.placeholders, state.canEdit, state.panelEdit])) return;
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
  state.canEdit = data.can_edit !== false;
  state.standalone = data.standalone === true;
  state.panelEdit = data.panel_edit === true;
  state.startScript = data.start_script || "";
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
  if (state.view === "form" && state.form?.dirty && !(await askConfirm(t("Discard unsaved changes?"), t("Discard"), true))) return false;
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
  if (state.probing.has(relay.name)) return el("span", { class: "chip" }, el("span", { class: "spinner" }), t("Testing"));
  if (!relay.configured) return el("span", { class: "chip warn" }, t("Not set up"));
  if (result?.ok && !result.models.length) return el("span", { class: "chip warn" }, t("● No models"));
  if (result?.ok) return el("span", { class: "chip ok" }, t("● OK"));
  if (result) return el("span", { class: "chip err" }, t("● Error"));
  return el("span", { class: "chip" }, t("Not tested"));
}

function render() {
  // 重新渲染会替换搜索框，记住它的焦点和光标，不然用户打字打到一半会丢
  const active = document.activeElement;
  const searchFocus = active?.dataset?.role === "model-search" ? [active.selectionStart, active.selectionEnd] : null;

  document.getElementById("cfgPath").textContent = state.configPath ? t("Config file: {path}", { path: state.configPath }) : "";
  document.getElementById("btnAdd").hidden = !state.canEdit;
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
    box.append(el("div", { class: "banner" }, el("span", {}, t("The manager backend has exited. Please reopen the manager."))));
    return;
  }
  if (!state.placeholders.length) return;
  box.append(
    el("div", { class: "banner" },
      el("span", {}, t("The config has {count} example relays that are not filled in ({names}).", { count: state.placeholders.length, names: state.placeholders.join(", ") })),
      state.canEdit ? el("button", { class: "btn small", onclick: removePlaceholders }, t("Remove examples")) : null),
  );
}

function renderList() {
  const box = document.getElementById("relayList");
  box.replaceChildren();
  if (state.loadError) {
    box.append(
      el("div", { class: "card result err" },
        el("div", { class: "title" }, t("Cannot read the config")),
        el("div", {}, state.loadError),
        el("div", { class: "note" }, t("If relays.json was broken by a manual edit, fix it and reload. The last saved version is relays.json.bak in the same folder.")),
        el("div", { class: "actions", style: "margin-top:8px" }, el("button", { class: "btn small", onclick: loadState }, t("Reload")))),
    );
    return;
  }
  if (!state.relays.length) {
    box.append(
      el("div", { class: "card empty" },
        el("div", { class: "big" }, t("No relays yet")),
        el("div", { class: "muted" }, state.canEdit ? t("Click Add relay and paste the address and token your relay gave you.") : readOnlyHint())),
    );
    if (state.standalone) box.append(panelEditToggle());
    return;
  }
  const configured = state.relays.filter((r) => r.configured);
  if (configured.length > 1) {
    box.append(
      el("div", { class: "actions", style: "margin-bottom:10px" },
        el("button", { class: "btn small", onclick: () => configured.forEach((r) => probe(r.name, true)) }, t("Test all"))),
    );
  }
  for (const relay of state.relays) {
    const result = state.probes[relay.name];
    let counts = relay.configured ? t("Click to view and test") : t("Address and token not filled in");
    if (result?.ok) counts = t("Text {text} · Image {image} · Video {video}", result.counts);
    else if (result) counts = t("Connection failed, click for details");
    const active = state.view === "detail" && state.selected === relay.name;
    box.append(
      el("div", { class: `card relay${active ? " active" : ""}`, onclick: () => select(relay.name) },
        el("div", { class: "row" }, el("div", { class: "name", title: relay.name }, relay.name), statusChip(relay, result)),
        el("div", { class: "host", title: relay.base_url }, relay.url_filled ? hostOf(relay.base_url) : t("(API address not filled in)")),
        el("div", { class: "counts" }, counts)),
    );
  }
  if (state.standalone) box.append(panelEditToggle());
}

function readOnlyHint() {
  const script = state.startScript;
  if (!script) return t("To add or change relays, run tools/start_manager on the computer running ComfyUI, or edit relays.json there by hand.");
  const windows = script.toLowerCase().endsWith(".bat");
  const command = windows ? script : `sh "${script}"`;
  return el("div", {},
    el("div", {}, t("To add or change relays, open the Relay Manager window by running this:")),
    el("div", { class: "mono", style: "margin:6px 0;overflow-wrap:anywhere" }, command),
    el("button", { class: "btn small", onclick: () => copyText(command) }, windows ? t("Copy path") : t("Copy command")),
    el("div", { style: "margin-top:6px" }, windows
      ? t("Paste it into the File Explorer address bar or the Run box (Win+R) and press Enter. You can also edit relays.json by hand.")
      : t("Run it in a terminal. You can also edit relays.json by hand.")));
}

function panelEditToggle() {
  return el("label", { class: "note", style: "display:flex;gap:8px;align-items:flex-start;cursor:pointer;margin-top:12px" },
    el("input", { type: "checkbox", checked: state.panelEdit, style: "width:auto;margin:2px 0 0", onchange: (e) => setPanelEdit(e.target.checked) }),
    el("span", {},
      el("div", {}, t("Allow changing relays in the ComfyUI sidebar")),
      el("div", { class: "muted" }, t("Off by default. When it is on, anything that reaches ComfyUI from this computer, such as a tunnel, could change where your prompts go."))));
}

function renderDetail(box) {
  if (state.loadError) return;
  const relay = state.relays.find((r) => r.name === state.selected);
  if (!relay) {
    box.append(el("div", { class: "card empty muted" }, state.relays.length ? t("Choose a relay on the left") : t("Its models show here after you add a relay")));
    return;
  }
  const result = state.probes[relay.name];
  const busy = state.probing.has(relay.name);
  const info = [
    el("dt", {}, t("API address")), el("dd", { class: "mono" }, relay.url_filled ? relay.base_url : t("Not filled in")),
    el("dt", {}, t("Token")), el("dd", { class: "mono" }, relay.key_masked || (relay.api_key_env ? t("Environment variable {name}", { name: relay.api_key_env }) : t("Not filled in"))),
    el("dt", {}, t("Proxy")), el("dd", {}, relay.proxy || t("None")),
  ];
  const software = result?.software;
  if (software?.site_name && software.site_name !== software.name) {
    info.push(el("dt", {}, t("Site name")), el("dd", {}, software.site_name));
  }
  if (software?.name) {
    info.push(el("dt", {}, t("Site software")), el("dd", {}, `${software.name} ${software.version || ""}`.trim()));
  }
  if (result?.ok) info.push(el("dt", {}, t("Response time")), el("dd", {}, `${result.latency_ms} ms`));

  box.append(
    el("div", { class: "card" },
      el("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:8px" },
        el("h2", { style: "min-width:0;overflow-wrap:anywhere" }, relay.name), statusChip(relay, result)),
      el("dl", { class: "info-grid" }, info),
      el("div", { class: "actions" },
        el("button", { class: "btn primary", disabled: busy || !relay.configured, onclick: () => probe(relay.name, true) },
          busy ? [el("span", { class: "spinner" }), " ", t("Testing")] : t("Test connection")),
        state.canEdit ? el("button", { class: "btn", onclick: () => openForm("edit", relay) }, relay.configured ? t("Edit") : t("Fill in")) : null,
        state.canEdit ? el("button", { class: "btn danger", onclick: () => removeRelay(relay) }, t("Delete")) : null),
      state.canEdit ? null : el("details", { class: "note" },
        el("summary", { style: "cursor:pointer" }, t("To change this relay, open the Relay Manager window")),
        el("div", { style: "margin-top:6px" }, readOnlyHint())),
      result && !result.ok ? el("div", { class: "result err" }, el("div", { class: "title" }, t("Connection failed")), result.error) : null,
      result?.ok && result.warnings?.length ? el("div", { class: "note" }, result.warnings.join("; ")) : null,
      result?.ok ? renderModels(relay, result) : null,
      EMBEDDED ? null : el("div", { class: "note" }, state.comfy.running
        ? t("Connected to the running ComfyUI (port {port}). Changes here sync to the relay list on the nodes.", { port: state.comfy.port })
        : t("No running ComfyUI detected. Once ComfyUI is running (a local instance on Comfy Desktop; Comfy Cloud cannot load plugins), the relays configured here appear in the relay list on the nodes."))),
  );
}

function renderModels(relay, result) {
  if (!result.models.length) {
    return el("div", { class: "result err" }, el("div", { class: "title" }, t("The relay returned no models")),
      t("The token may lack group permissions, or the relay has no models listed yet. Check the token settings in the relay's console."));
  }
  const list = el("div", { class: "models", style: "margin-top:10px" });
  const refresh = () => {
    const query = state.search.trim().toLowerCase();
    const items = result.models.filter(
      (m) => (state.filter === "all" || m.kind === state.filter) && (!query || m.id.toLowerCase().includes(query)),
    );
    const rows = items.slice(0, MAX_ROWS).map((m) => modelRow(relay, m));
    if (!rows.length) rows.push(el("div", { class: "empty muted" }, t("No matching models")));
    if (items.length > MAX_ROWS) rows.push(el("div", { class: "empty muted" }, t("{count} more. Use the search to narrow them down", { count: items.length - MAX_ROWS })));
    list.replaceChildren(...rows);
  };
  const search = el("input", {
    ...TEXT_INPUT,
    "data-role": "model-search",
    placeholder: t("Search model names, such as banana, sora, gpt"),
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
      }, kind === "all" ? `${t("All")} ${result.models.length}` : `${KIND_LABEL[kind]} ${result.counts[kind] ?? 0}`)),
  );
  refresh();
  return el("div", {},
    el("h3", {}, t("Models")),
    el("div", { class: "toolbar" }, search),
    filters,
    list,
    canvasAvailable()
      ? el("div", { class: "note" }, t("Add to canvas places a configured node in the middle of the ComfyUI canvas with the recommended API format. You can change the format on the node if it does not work."))
      : null,
  );
}

function modelRow(relay, model) {
  const key = `${relay.name}|${model.id}`;
  const reply = state.replies[key];
  const actions = el("div", { class: "actions" });
  if (model.kind === "text" && state.canEdit) {
    actions.append(el("button", { class: "btn small", disabled: reply?.busy, onclick: () => chatTest(relay, model) },
      reply?.busy ? t("Testing…") : t("Try it")));
  }
  if (canvasAvailable() && NODE_FOR_KIND[model.kind]) {
    actions.append(el("button", { class: "btn small primary", onclick: () => addToCanvas(relay, model) }, t("Add to canvas")));
  }
  actions.append(el("button", { class: "btn small", onclick: () => copyText(model.id) }, t("Copy")));
  return el("div", { class: "model" },
    el("div", { class: "id", title: model.id }, model.id),
    el("div", { class: "meta" },
      el("span", { class: `chip kind-${model.kind}` }, KIND_LABEL[model.kind] || model.kind),
      model.api_format
        ? el("span", {
          class: "chip",
          title: [model.reason, model.endpoint_types?.length ? t("Marked by the relay: {types}", { types: model.endpoint_types.join(", ") }) : ""]
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
    state.replies[key] = { ok: true, text: `${res.reply || t("(empty reply)")} (${res.latency_ms} ms)` };
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
    toast(t("Sent to the ComfyUI canvas: {model}", { model: model.id }), "ok");
  } catch (e) {
    toast(e.message, "err");
    checkComfy();
  }
}

async function setPanelEdit(enabled) {
  try {
    await api("panel_edit", { method: "POST", body: { enabled } });
    state.panelEdit = enabled;
    notifyChanged();
  } catch (e) {
    toast(e.message, "err");
  }
  render();
}

async function removeRelay(relay) {
  if (!(await askConfirm(t("Delete relay \"{name}\"? Workflows that use it will need another relay.", { name: relay.name }), t("Delete"), true))) return;
  try {
    await api(`relays/${encodeURIComponent(relay.name)}`, { method: "DELETE" });
    toast(t("Deleted \"{name}\"", { name: relay.name }), "ok");
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
    toast(res.removed.length ? t("Deleted: {names}", { names: res.removed.join(", ") }) : t("No examples to delete"), "ok");
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
    placeholder: t("Paste the address and token your relay gave you here"),
    oninput: (e) => {
      form.dirty = true;
      clearTimeout(timer);
      const text = e.target.value;
      timer = setTimeout(() => quickParse(form, text), 250);
    },
  });

  refs.key = input("api_key", {
    type: "password",
    placeholder: form.hasKey ? t("Leave empty to keep the current token ({token})", { token: relay.key_masked || t("Environment variable {name}", { name: relay.api_key_env }) }) : "sk-...",
  });
  const toggle = el("button", {
    class: "btn",
    type: "button",
    onclick: () => {
      refs.key.type = refs.key.type === "password" ? "text" : "password";
      toggle.textContent = refs.key.type === "password" ? t("Show") : t("Hide");
    },
  }, t("Show"));

  refs.status = el("div");
  refs.test = el("button", { class: "btn", onclick: () => testForm(form) }, t("Test connection"));
  refs.save = el("button", { class: "btn primary", onclick: () => saveForm(form) }, form.mode === "add" ? t("Save") : t("Save changes"));

  if (!state.standalone) refs.test.hidden = true; // 测试没保存的地址只有独立窗口能做；在这里保存后会自动测试
  return el("div", { class: "card" },
    el("h2", {}, form.mode === "add" ? t("Add relay") : t("Edit \"{name}\"", { name: relay.name })),
    form.hasKey ? null : field(t("Quick paste"), refs.paste, t("For example: API address https://api.xxx.com/v1  token sk-xxxx. The fields below fill in automatically")),
    field(t("Name"), input("name", { placeholder: t("Anything you like, such as closeai or backup") }), t("Shown in the relay list on the nodes")),
    field(t("API address"), input("base_url", { placeholder: "https://api.xxx.com/v1" }), t("With or without /v1. A pasted /chat/completions is removed automatically")),
    field(t("Token (API key)"), el("div", { class: "key-row" }, refs.key, toggle)),
    field(t("Proxy (optional)"), input("proxy", { placeholder: t("Only if you need one to reach the relay, such as http://127.0.0.1:7890") })),
    el("div", { class: "actions" },
      refs.test,
      refs.save,
      el("button", {
        class: "btn",
        onclick: async () => {
          if (await leaveForm()) render();
        },
      }, t("Cancel"))),
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
      found.push(t("API address"));
    }
    if (parsed.api_key) {
      v.api_key = parsed.api_key;
      found.push(t("Token"));
    }
    if (parsed.proxy) {
      v.proxy = parsed.proxy;
      found.push(t("Proxy"));
    }
    if (parsed.name && (form.autoName || !v.name)) {
      v.name = parsed.name;
      form.autoName = true;
    }
    for (const key of ["name", "base_url", "proxy"]) form.refs[key].value = v[key];
    form.refs.key.value = v.api_key;
    setFormStatus(form, found.length
      ? { ok: true, title: t("Detected: {items}", { items: found.join(", ") }), detail: state.standalone ? t("Check them, then test the connection or save") : t("Check them, then save") }
      : { ok: false, title: t("No address or token detected"), detail: t("You can fill in the fields below by hand") });
  } catch (e) {
    setFormStatus(form, { ok: false, title: t("Detection failed"), detail: e.message });
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
  setBusy(form, true, t("Testing the connection…"));
  try {
    const res = await api("test", {
      method: "POST",
      body: { ...form.values, keep_key_of: form.mode === "edit" ? form.original : null },
    });
    if (res.ok && !res.models.length) {
      setFormStatus(form, { ok: false, title: t("Connected, but the relay returned no models"), detail: (res.warnings || []).join("; ") });
    } else if (res.ok) {
      const software = res.software?.name ? ` · ${res.software.name} ${res.software.version || ""}` : "";
      const warnings = res.warnings?.length ? `\n${res.warnings.join("; ")}` : "";
      setFormStatus(form, {
        ok: true,
        title: t("Connected, {count} models", { count: res.models.length }),
        detail: `${t("Text {text} · Image {image} · Video {video}", res.counts)}${software} · ${res.latency_ms} ms${warnings}`,
      });
    } else {
      setFormStatus(form, { ok: false, title: t("Connection failed"), detail: res.error });
    }
  } catch (e) {
    setFormStatus(form, { ok: false, title: t("Please check"), detail: e.message }); // 没发出请求，是填写的内容有问题
  } finally {
    setBusy(form, false);
  }
}

async function saveForm(form) {
  const renaming = form.mode === "edit" && form.values.name.trim() !== form.original;
  if (renaming && !(await askConfirm(t("After renaming, workflows that use \"{name}\" will need another relay. Rename it?", { name: form.original }), t("Rename")))) return;
  setBusy(form, true, t("Saving…"));
  const body = { ...form.values };
  try {
    const res = form.mode === "add"
      ? await api("relays", { method: "POST", body })
      : await api(`relays/${encodeURIComponent(form.original)}`, { method: "PUT", body });
    const name = res.relay.name;
    toast(form.mode === "add" ? t("Added \"{name}\"", { name }) : t("Saved \"{name}\"", { name }), "ok");
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
    setFormStatus(form, { ok: false, title: t("Save failed"), detail: e.message });
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
      setFormStatus(form, { ok: true, title: t("Restored your unsaved input"), detail: t("The panel was closed for a moment. Please check it before saving") });
    },
  };
}

window.addEventListener("message", (event) => {
  if (!EMBEDDED || event.source !== window.parent || event.origin !== location.origin) return;
  const msg = event.data;
  if (!msg || msg.source !== "relay-api-comfy") return;
  if (msg.type === "add-node-result") {
    toast(msg.ok ? t("Added to canvas: {title}", { title: msg.title }) : t("Could not add it: {error}", { error: msg.error }), msg.ok ? "ok" : "err");
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

document.title = t("Relay Manager");
document.getElementById("title").textContent = t("Relay Manager");
document.getElementById("cfgPath").textContent = t("Loading…");
const addButton = document.getElementById("btnAdd");
addButton.textContent = t("＋ Add relay");
addButton.addEventListener("click", () => openForm("add"));
if (!EMBEDDED) {
  // 独立窗口没有 ComfyUI 的语言设置可跟随，默认英文，用这个开关切换并记住
  const langButton = document.getElementById("btnLang");
  langButton.hidden = false;
  langButton.textContent = LANG === "zh" ? "English" : "中文";
  langButton.addEventListener("click", () => {
    try {
      localStorage.setItem(LANG_KEY, LANG === "zh" ? "en" : "zh");
    } catch {
      /* 存不了就只是下次还是默认语言 */
    }
    location.reload();
  });
  await api("language", { method: "POST", body: { lang: LANG } }).catch(() => {}); // 后端的报错文字也用同一种语言
}
started = loadState();
