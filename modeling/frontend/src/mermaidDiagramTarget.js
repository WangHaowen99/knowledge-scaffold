// 知识包里的 diagrams/<流程名称>.mmd 是 diagrams/processes.json 的派生产物；
// 历史 diagrams/process_N.mmd 继续按序号兼容读取。
// 点击 .mmd 时读结构化源文档交给流程图编辑器，而不是解析 mermaid 文本。
const PROCESS_MMD_PATTERN = /^diagrams\/process_(\d+)\.mmd$/u;
const DIAGRAM_MMD_PATTERN = /^diagrams\/[^/]+\.mmd$/u;

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
  return DIAGRAM_MMD_PATTERN.test(String(path ?? ''));
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
  if (!isProcessDiagramPath(path)) return null;
  if (!hasDiagramDocument(files)) return null;
  const document = parseDiagramDocument(documentContent);
  if (!document) return null;
  const namedIndex = document.diagrams.findIndex((diagram) => (
    `diagrams/${String(diagram?.title ?? '').normalize('NFC').trim()}.mmd` === path
  ));
  const index = namedIndex >= 0 ? namedIndex : diagramIndexFromPath(path);
  if (index === null) return null;
  const activeDiagramId = diagramIdAt(document, index);
  // .mmd 存在但无法关联到源文档中的流程图，交回原文视图，避免错误回写。
  if (!activeDiagramId) return null;
  return { index, document, activeDiagramId };
}
