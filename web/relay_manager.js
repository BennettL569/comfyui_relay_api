// ComfyUI 左侧栏的「中转站」面板：嵌入管理器页面；同时接收面板和桌面独立管理器发来的通知，
// 把配好的节点加到画布上、刷新节点的「中转站」下拉框。
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TAB_ID = "relay-api-manager";
const NODE_TYPES = ["RelayAPI_Chat", "RelayAPI_Image", "RelayAPI_Video"];
let frame = null;
let panel = null; // 当前打开着的面板容器
let parking = null; // 面板关着时 iframe 藏在这里
let draft = null; // iframe 不得不重新加载时，暂存里面没保存的表单（只放内存）

function toast(severity, summary, detail = "") {
  app.extensionManager?.toast?.add({ severity, summary, detail, life: 4000 });
}

async function refreshNodeDefinitions() {
  // 中转站增删改后，让节点的「中转站」下拉框立刻更新（和按 R 一样）
  const command = app.extensionManager?.command;
  if (command?.execute) await command.execute("Comfy.RefreshNodeDefinitions");
  else await app.refreshComboInNodes?.();
}

function setWidget(node, name, value) {
  const widget = node.widgets?.find((w) => w.name === name);
  if (!widget) return false;
  // 下拉框只接受列表里有的值；动态下拉框（接口格式）赋值后会自动换出对应的子参数
  const options = widget.options?.values;
  if (Array.isArray(options) && !options.includes(value)) return false;
  widget.value = value;
  widget.callback?.(value, app.canvas, node);
  return true;
}

function relayOptions(type) {
  return window.LiteGraph?.registered_node_types?.[type]?.nodeData?.input?.required?.relay?.[1]?.options;
}

function addNode(spec) {
  const canvas = app.canvas;
  const graph = canvas?.graph ?? app.graph; // 在子图里时加到当前看到的子图
  const node = window.LiteGraph?.createNode(spec.node);
  if (!node) throw new Error(`找不到节点类型 ${spec.node}，请重启 ComfyUI`);
  const [vx, vy, vw, vh] = canvas.ds.visible_area;
  node.pos = [vx + vw / 2 - node.size[0] / 2, vy + vh / 2 - node.size[1] / 2];
  graph.add(node);
  if (!setWidget(node, "relay", spec.relay)) {
    graph.remove(node); // 不能悄悄换成别的中转站
    throw new Error(`ComfyUI 里还没有中转站「${spec.relay}」，请在面板里刷新后再试`);
  }
  setWidget(node, "model", spec.model);
  if (spec.api_format) setWidget(node, "api_format", spec.api_format);
  canvas.selectNode?.(node);
  canvas.setDirty?.(true, true);
  // iframe 里的点击不会触发画布的撤销记录，手动记一下，Ctrl+Z 才能撤销
  app.extensionManager?.workflow?.activeWorkflow?.changeTracker?.captureCanvasState?.();
  return node;
}

async function handle(msg) {
  if (msg?.type === "relays-changed") {
    await refreshNodeDefinitions();
    return null;
  }
  if (msg?.type === "add-node") {
    if (!NODE_TYPES.includes(msg.node)) throw new Error("不支持的节点类型");
    if (!relayOptions(msg.node)?.includes(msg.relay)) await refreshNodeDefinitions(); // 刚添加的中转站还不在下拉框里
    return addNode(msg);
  }
  return null;
}

function reply(message) {
  frame?.contentWindow?.postMessage({ source: "relay-api-comfy", ...message }, location.origin);
}

// 侧边栏面板（同源 iframe）发来的消息
window.addEventListener("message", async (event) => {
  if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin) return;
  const msg = event.data;
  if (msg?.source !== "relay-api-manager") return;
  try {
    const node = await handle(msg);
    if (node) reply({ type: "add-node-result", ok: true, title: node.title || msg.node });
  } catch (e) {
    console.error("[中转站]", e);
    if (msg.type === "add-node") reply({ type: "add-node-result", ok: false, error: e.message });
    else toast("error", "中转站", e.message);
  }
});

// 桌面独立管理器经由服务端转发过来的通知
api.addEventListener("relay_api.event", async ({ detail }) => {
  try {
    const node = await handle(detail);
    if (detail?.type === "relays-changed") reply({ type: "reload" }); // 面板开着的话也刷新列表
    if (node) toast("success", "中转站", `已添加节点：${node.title || detail.node}（${detail.model}）`);
  } catch (e) {
    console.error("[中转站]", e);
    toast("error", "中转站", e.message);
  }
});

// ComfyUI 每次打开面板都给一个新容器，关面板时连容器带 iframe 一起删掉，iframe 一离开页面就会重新加载。
// 支持 moveBefore 的浏览器（Chromium 133+，ComfyUI 桌面版也是）能把 iframe 原样挪走再挪回来，不刷新；
// 不支持的只能重新加载：先把没保存的表单取出来，加载完再交回去。
const canMove = () => typeof Element.prototype.moveBefore === "function";

function keepDraft() {
  try {
    const win = frame?.contentWindow; // 已经销毁的 iframe 是 null，保留之前取到的
    if (win) draft = win.relayManager?.draft?.() ?? null;
  } catch (e) {
    console.warn("[中转站]", e);
  }
}

function createFrame() {
  const iframe = document.createElement("iframe");
  iframe.src = `${api.api_base ?? ""}/relay_api/manager/?embedded=1`;
  iframe.title = "中转站管理器";
  iframe.style.cssText = "width:100%;height:100%;border:0;display:block;background:transparent";
  iframe.addEventListener("load", () => {
    if (!draft) return;
    const saved = draft;
    draft = null;
    iframe.contentWindow?.relayManager?.restore?.(saved);
  });
  return iframe;
}

function attach(container, tries = 0) {
  if (panel !== container || frame.parentNode === container) return;
  if (frame.isConnected && canMove()) {
    if (!container.isConnected && tries < 20) {
      // 新面板这时还没放进页面（挪动要求两边都在页面里），等这一轮界面更新完再挪
      if (tries === 0) queueMicrotask(() => attach(container, 1));
      else setTimeout(() => attach(container, tries + 1), 50);
      return;
    }
    try {
      container.moveBefore(frame, null);
      return;
    } catch (e) {
      console.warn("[中转站]", e);
    }
  }
  keepDraft();
  container.replaceChildren(frame);
}

function park() {
  if (!frame?.isConnected) return;
  if (canMove()) {
    if (!parking) {
      parking = document.createElement("div");
      parking.style.display = "none";
      document.body.append(parking);
    }
    try {
      parking.moveBefore(frame, null);
      return;
    } catch (e) {
      console.warn("[中转站]", e);
    }
  }
  keepDraft(); // iframe 会跟着面板一起销毁
}

app.registerExtension({
  name: "comfyui_relay_api.Manager",
  setup() {
    app.extensionManager?.registerSidebarTab?.({
      id: TAB_ID,
      icon: "pi pi-link",
      title: "中转站",
      tooltip: "中转站管理器：添加第三方 API、测试连接、把模型节点加到画布",
      type: "custom",
      render: (container) => {
        container.style.height = "100%";
        panel = container;
        frame ??= createFrame();
        attach(container);
      },
      destroy: () => {
        // 面板关掉前（容器还在页面里）把 iframe 藏起来，下次打开原样挪回去
        panel = null;
        park();
      },
    });
    // 切到别的侧边栏页时 ComfyUI 不调 destroy，直接把面板删掉；所以一切换（界面更新之前）就先藏起来
    const tabs = app.extensionManager?.sidebarTab;
    tabs?.$subscribe?.(() => {
      if (panel && tabs.activeSidebarTabId !== TAB_ID) {
        panel = null;
        park();
      }
    }, { flush: "sync", detached: true });
  },
});
