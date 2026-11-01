// 在「文本对话」「模型列表」节点上直接显示结果，复用内置「预览为文本」节点的同一套组件。
import { app } from "../../scripts/app.js";

const NODES = ["RelayAPI_Chat", "RelayAPI_Models"];

// 子图里的节点 id 形如「子图 uuid:节点号」
function findNode(root, id) {
  const key = String(id);
  const split = key.lastIndexOf(":");
  if (split < 0) return root.getNodeById(key);
  return root.subgraphs?.get?.(key.slice(0, split))?.getNodeById(key.slice(split + 1)) ?? null;
}

app.registerExtension({
  name: "comfyui_relay_api.ShowText",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    const preview = window.comfyAPI?.textPreviewWidgets;
    if (!preview || !NODES.includes(nodeData.name)) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      preview.addTextPreviewWidgets(this);
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      preview.updateTextPreviewWidgets(this, message);
    };
  },

  // 切换工作流标签页回来时恢复显示的结果
  onNodeOutputsUpdated(outputs) {
    const preview = window.comfyAPI?.textPreviewWidgets;
    const root = app.rootGraph ?? app.graph;
    if (!preview || !root) return;
    for (const [id, output] of Object.entries(outputs)) {
      const node = findNode(root, id);
      if (node && NODES.includes(node.type)) preview.updateTextPreviewWidgets(node, output);
    }
  },
});
