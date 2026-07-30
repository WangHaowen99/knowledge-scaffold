// 知识包里的 diagrams/process_N.mmd 是 diagrams/processes.json 的派生产物：
// 点击 .mmd 时读结构化源文档交给流程图编辑器，而不是解析 mermaid 文本。
const PROCESS_MMD_PATTERN = /^diagrams\/process_(\d+)\.mmd$/u;

export const DIAGRAM_DOCUMENT_PATH = 'diagrams/processes.json';

// 返回 0 基图序号；不是流程图文件时返回 null。
export function diagramIndexFromPath(path) {
  const matched = PROCESS_MMD_PATTERN.exec(String(path ?? ''));
  if (!matched) return null;
  const ordinal = Number(matched[1]);
  // process_0.mmd 不由 renderDiagramFiles 产出，视为普通文件走原文视图
  if (!Number.isInteger(ordinal) || ordinal < 1) return null;
  return ordinal - 1;
}

export function isProcessDiagramPath(path) {
  return diagramIndexFromPath(path) !== null;
}

// 文件树里是否存在配套源文档；缺失时调用方降级为原文视图。
export function hasDiagramDocument(files) {
  return Array.isArray(files) && files.includes(DIAGRAM_DOCUMENT_PATH);
}

export function parseDiagramDocument(content) {
  let parsed;
  try {
    parsed = JSON.parse(String(content ?? ''));
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  if (!Array.isArray(parsed.diagrams)) return null;
  return parsed;
}

// 定位点击的那张图，供编辑器打开时直接激活。
export function diagramIdAt(document, index) {
  if (!document || !Array.isArray(document.diagrams)) return '';
  const diagram = document.diagrams[index];
  return diagram && typeof diagram.id === 'string' ? diagram.id : '';
}

// 一次性给出 .mmd 的可编辑目标；任何一环缺失都返回 null，由调用方降级。
export function resolveDiagramTarget({ path, files, documentContent }) {
  const index = diagramIndexFromPath(path);
  if (index === null) return null;
  if (!hasDiagramDocument(files)) return null;
  const document = parseDiagramDocument(documentContent);
  if (!document) return null;
  const activeDiagramId = diagramIdAt(document, index);
  // .mmd 存在但源文档里没有第 N 张图，说明两者已不同步，交回原文视图更诚实
  if (!activeDiagramId) return null;
  return { index, document, activeDiagramId };
}
