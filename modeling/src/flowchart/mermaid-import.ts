import { createHash } from 'node:crypto';

import { appendFlowchartDiagram, validateFlowchartDocument } from './document.js';
import { diagramFilePath } from './file-path.js';
import type {
  FlowchartDiagram,
  FlowchartDocument,
  FlowchartEdge,
  FlowchartNode,
  FlowchartNodeKind,
  FlowchartValidationIssue,
} from './types.js';

export const MERMAID_IMPORT_MAX_BYTES = 1024 * 1024;
export const MERMAID_IMPORT_MAX_NODES = 500;
export const MERMAID_IMPORT_MAX_EDGES = 1000;

export type MermaidImportErrorCategory =
  | '内容错误'
  | '图表类型不支持'
  | 'Mermaid 语法错误'
  | '安全限制'
  | '规模超限'
  | '流程名称错误';

export interface MermaidImportDiagnostic {
  code: string;
  category: MermaidImportErrorCategory;
  message: string;
  suggestion: string;
  line?: number;
  column?: number;
  sourceLine?: string;
}

export class MermaidImportError extends Error {
  constructor(public readonly diagnostic: MermaidImportDiagnostic) {
    super(diagnostic.message);
    this.name = 'MermaidImportError';
  }
}

export interface MermaidImportNotice {
  code: 'subgraph_flattened' | 'reverse_direction_preserved';
  message: string;
}

export interface MermaidImportResult {
  diagram: FlowchartDiagram;
  document: FlowchartDocument;
  warnings: FlowchartValidationIssue[];
  notices: MermaidImportNotice[];
  summary: {
    diagramType: 'flowchart' | 'graph';
    direction: 'TB' | 'BT' | 'LR' | 'RL';
    nodeCount: number;
    edgeCount: number;
    subgraphCount: number;
  };
}

export interface ImportMermaidFlowchartInput {
  title: unknown;
  source: unknown;
  filename?: unknown;
  diagramId?: string;
}

interface SourcePosition {
  line: number;
  column: number;
  sourceLine: string;
}

interface ParsedNode extends FlowchartNode {
  explicit: boolean;
  position: SourcePosition;
}

interface ParsedMermaid {
  diagramType: 'flowchart' | 'graph';
  direction: 'TB' | 'BT' | 'LR' | 'RL';
  nodes: FlowchartNode[];
  edges: FlowchartEdge[];
  subgraphCount: number;
}

interface Statement {
  text: string;
  line: number;
  column: number;
  sourceLine: string;
}

const HEADER = /^(flowchart|graph)\s+(TB|TD|BT|LR|RL)\s*;?$/iu;
const SUPPORTED_FILE = /\.(?:mmd|mermaid)$/iu;
const NODE_ID = /^[A-Za-z_][A-Za-z0-9_-]*/u;
const FORBIDDEN_STATEMENT = /^(?:click|classDef|class|style|linkStyle|accTitle|accDescr)\b/iu;
const CONTROL_CHAR = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u;
const SHAPES: Array<{ open: string; close: string; kind: FlowchartNodeKind }> = [
  { open: '([', close: '])', kind: 'action' },
  { open: '[[', close: ']]', kind: 'action' },
  { open: '[(', close: ')]', kind: 'data' },
  { open: '((', close: '))', kind: 'action' },
  { open: '{{', close: '}}', kind: 'decision' },
  { open: '[/', close: '/]', kind: 'data' },
  { open: '[\\', close: '\\]', kind: 'data' },
  { open: '[/', close: '\\]', kind: 'data' },
  { open: '[\\', close: '/]', kind: 'data' },
  { open: '(', close: ')', kind: 'action' },
  { open: '[', close: ']', kind: 'action' },
  { open: '{', close: '}', kind: 'decision' },
  { open: '>', close: ']', kind: 'action' },
];

export function importMermaidFlowchart(
  current: FlowchartDocument,
  input: ImportMermaidFlowchartInput,
): MermaidImportResult {
  const title = requiredTitle(input.title);
  const source = requiredSource(input.source);
  const filename = optionalFilename(input.filename);
  assertUniqueTitle(current, title);
  const parsed = parseMermaid(source);
  const diagram: FlowchartDiagram = {
    id: input.diagramId?.trim() || stableDiagramId(title, source),
    title,
    nodes: parsed.nodes,
    edges: parsed.edges,
    layout: { direction: parsed.direction, positions: {} },
    importSource: {
      kind: 'mermaid',
      diagramType: parsed.diagramType,
      ...(filename ? { filename } : {}),
    },
  };
  let document: FlowchartDocument;
  try {
    document = appendFlowchartDiagram(current, diagram);
  } catch (cause) {
    throw nameError(cause instanceof Error ? cause.message : '流程名称或流程数量不符合要求');
  }
  const issues = validateFlowchartDocument(document).filter((issue) => issue.diagramId === diagram.id);
  const error = issues.find((issue) => issue.severity === 'error');
  if (error) throw nameError(error.message);
  const notices: MermaidImportNotice[] = [];
  if (parsed.subgraphCount) {
    notices.push({
      code: 'subgraph_flattened',
      message: `已读取 ${parsed.subgraphCount} 个 subgraph；导入后保留其中的节点和连线，分组边框不会进入结构化流程。`,
    });
  }
  if (parsed.direction === 'BT' || parsed.direction === 'RL') {
    notices.push({
      code: 'reverse_direction_preserved',
      message: `已保留 ${parsed.direction} 布局方向；流程语义仍以连线箭头方向为准。`,
    });
  }
  return {
    diagram: document.diagrams.at(-1)!,
    document,
    warnings: issues.filter((issue) => issue.severity === 'warning'),
    notices,
    summary: {
      diagramType: parsed.diagramType,
      direction: parsed.direction,
      nodeCount: parsed.nodes.length,
      edgeCount: parsed.edges.length,
      subgraphCount: parsed.subgraphCount,
    },
  };
}

function parseMermaid(source: string): ParsedMermaid {
  const lines = source.replace(/^\uFEFF/u, '').split(/\r?\n/u);
  const initLineIndex = lines.findIndex((line) => line.trim().startsWith('%%{'));
  if (initLineIndex >= 0) {
    throw atLine(
      'MERMAID_INIT_DIRECTIVE_FORBIDDEN', '安全限制', '不允许使用 Mermaid 初始化指令',
      '请删除 %%{init: ...}%% 指令，只保留 flowchart/graph 图定义。',
      initLineIndex, lines, lines[initLineIndex].indexOf('%%{'),
    );
  }
  const headerIndex = lines.findIndex((line) => {
    const text = line.trim();
    return Boolean(text) && !text.startsWith('%%');
  });
  if (headerIndex < 0) {
    throw diagnostic('MERMAID_EMPTY', '内容错误', 'Mermaid 内容为空', '请粘贴或选择包含 flowchart/graph 的 Mermaid 文件。');
  }
  const headerText = lines[headerIndex].trim();
  const header = HEADER.exec(headerText);
  if (!header) {
    const type = /^([A-Za-z][A-Za-z0-9_-]*)/u.exec(headerText)?.[1] ?? '';
    if (type && !['flowchart', 'graph'].includes(type.toLocaleLowerCase('en-US'))) {
      throw atLine(
        'MERMAID_DIAGRAM_TYPE_UNSUPPORTED', '图表类型不支持',
        `仅支持 flowchart/graph，当前图表类型为 ${type}`,
        '请将文件转换为 flowchart 或 graph，并在首行声明布局方向，例如 flowchart TD。',
        headerIndex, lines, lines[headerIndex].indexOf(type),
      );
    }
    throw atLine(
      'MERMAID_HEADER_INVALID', 'Mermaid 语法错误', 'Mermaid 首行格式不正确',
      '首个有效行必须类似 flowchart TD、graph LR，并使用 TB/TD/BT/LR/RL 方向。',
      headerIndex, lines, Math.max(0, lines[headerIndex].search(/\S/u)),
    );
  }

  const diagramType = header[1].toLocaleLowerCase('en-US') as 'flowchart' | 'graph';
  const rawDirection = header[2].toLocaleUpperCase('en-US');
  const direction = (rawDirection === 'TD' ? 'TB' : rawDirection) as ParsedMermaid['direction'];
  const nodes = new Map<string, ParsedNode>();
  const edges: FlowchartEdge[] = [];
  let subgraphDepth = 0;
  let subgraphCount = 0;

  for (let lineIndex = headerIndex + 1; lineIndex < lines.length; lineIndex += 1) {
    const rawLine = lines[lineIndex];
    const trimmed = rawLine.trim();
    if (!trimmed || trimmed.startsWith('%%') && !trimmed.startsWith('%%{')) continue;
    if (trimmed.startsWith('%%{')) {
      throw atLine(
        'MERMAID_INIT_DIRECTIVE_FORBIDDEN', '安全限制', '不允许使用 Mermaid 初始化指令',
        '请删除 %%{init: ...}%% 指令。', lineIndex, lines, rawLine.indexOf('%%{'),
      );
    }
    for (const statement of splitStatements(rawLine, lineIndex)) {
      const text = statement.text.trim();
      if (!text) continue;
      if (FORBIDDEN_STATEMENT.test(text) || /:::/u.test(text)) {
        throw statementError(
          'MERMAID_DIRECTIVE_FORBIDDEN', '安全限制',
          `不支持或不允许该 Mermaid 指令：${text.split(/\s/u, 1)[0]}`,
          '请删除 click、style、class、链接或可执行指令，只保留节点、连线、direction 和 subgraph。',
          statement,
        );
      }
      if (/^subgraph(?:\s|$)/iu.test(text)) {
        if (!/^subgraph\s+.+$/iu.test(text)) {
          throw statementError(
            'MERMAID_SUBGRAPH_INVALID', 'Mermaid 语法错误', 'subgraph 缺少名称',
            '请使用 subgraph 分组名称，并以 end 结束分组。', statement,
          );
        }
        subgraphDepth += 1;
        subgraphCount += 1;
        continue;
      }
      if (/^end$/iu.test(text)) {
        if (!subgraphDepth) {
          throw statementError(
            'MERMAID_SUBGRAPH_END_UNEXPECTED', 'Mermaid 语法错误', '发现没有对应 subgraph 的 end',
            '请删除多余的 end，或在前面补充 subgraph 声明。', statement,
          );
        }
        subgraphDepth -= 1;
        continue;
      }
      if (/^direction\b/iu.test(text)) {
        const nestedDirection = /^direction\s+(TB|TD|BT|LR|RL)$/iu.exec(text);
        if (!nestedDirection) {
          throw statementError(
            'MERMAID_DIRECTION_INVALID', 'Mermaid 语法错误', 'subgraph 布局方向不正确',
            'direction 只支持 TB、TD、BT、LR 或 RL。', statement,
          );
        }
        continue;
      }
      parseGraphStatement(statement, nodes, edges);
    }
  }
  if (subgraphDepth) {
    throw diagnostic(
      'MERMAID_SUBGRAPH_UNCLOSED', 'Mermaid 语法错误',
      `有 ${subgraphDepth} 个 subgraph 未使用 end 结束`, '请在文件末尾为每个 subgraph 补充对应的 end。',
    );
  }
  if (!nodes.size) {
    throw diagnostic(
      'MERMAID_NO_NODES', '内容错误', 'Mermaid 流程图中没有可导入的节点',
      '请至少添加一个节点，例如 start[开始]。',
    );
  }
  inferTerminalKinds(nodes, edges);
  for (const edge of edges) {
    if (edge.kind === 'sequence' && nodes.get(edge.from)?.kind === 'decision') edge.kind = 'condition';
  }
  return {
    diagramType,
    direction,
    nodes: [...nodes.values()].map(({ explicit: _explicit, position: _position, ...node }) => node),
    edges,
    subgraphCount,
  };
}

function parseGraphStatement(
  statement: Statement,
  nodes: Map<string, ParsedNode>,
  edges: FlowchartEdge[],
): void {
  let cursor = 0;
  const first = parseNode(statement, cursor);
  registerNode(nodes, first.node, first.explicit, positionAt(statement, first.start));
  cursor = first.cursor;
  let previousId = first.node.id;
  let foundEdge = false;
  while (true) {
    cursor = skipWhitespace(statement.text, cursor);
    if (cursor >= statement.text.length) break;
    const parsedEdge = parseEdge(statement, cursor);
    cursor = parsedEdge.cursor;
    const target = parseNode(statement, cursor);
    registerNode(nodes, target.node, target.explicit, positionAt(statement, target.start));
    const edge: FlowchartEdge = {
      id: `edge-${edges.length + 1}`,
      from: previousId,
      to: target.node.id,
      kind: parsedEdge.kind,
      ...(parsedEdge.label ? { label: parsedEdge.label, conditionText: parsedEdge.label } : {}),
    };
    edges.push(edge);
    if (edges.length > MERMAID_IMPORT_MAX_EDGES) {
      throw statementError(
        'MERMAID_EDGE_LIMIT', '规模超限',
        `连线数量超过上限 ${MERMAID_IMPORT_MAX_EDGES} 条`,
        `请拆分流程，确保单次导入不超过 ${MERMAID_IMPORT_MAX_EDGES} 条连线。`, statement,
      );
    }
    foundEdge = true;
    previousId = target.node.id;
    cursor = target.cursor;
  }
  if (!foundEdge && !first.explicit) {
    throw statementError(
      'MERMAID_NODE_DECLARATION_INCOMPLETE', 'Mermaid 语法错误',
      `节点 ${first.node.id} 缺少显示文字`,
      `请使用 ${first.node.id}[节点名称] 声明节点，或将它放在一条连线中。`, statement,
    );
  }
}

function parseNode(statement: Statement, from: number) {
  const cursor = skipWhitespace(statement.text, from);
  const match = NODE_ID.exec(statement.text.slice(cursor));
  if (!match) {
    throw syntaxAt(statement, cursor, '无法识别节点 ID', '节点 ID 必须以英文字母或下划线开头，只包含字母、数字、下划线和连字符。');
  }
  const id = match[0];
  let end = cursor + id.length;
  const shape = SHAPES.find((candidate) => statement.text.startsWith(candidate.open, end));
  if (!shape) return {
    node: { id, label: id, kind: 'action' as const }, explicit: false, start: cursor, cursor: end,
  };
  const contentStart = end + shape.open.length;
  const contentEnd = findClosing(statement.text, contentStart, shape.close);
  if (contentEnd < 0) {
    throw syntaxAt(
      statement, end, `节点 ${id} 的形状没有闭合`,
      `请在节点文字后补充 ${shape.close}。`,
    );
  }
  const rawLabel = statement.text.slice(contentStart, contentEnd);
  const quoted = /^\s*(["']).*\1\s*$/su.test(rawLabel);
  if (!quoted && /(?:-->|-.->|==>|[A-Za-z_][A-Za-z0-9_-]*\s*[\[({])/u.test(rawLabel)) {
    throw syntaxAt(
      statement, end, `节点 ${id} 的形状没有闭合`,
      `请在节点文字后补充 ${shape.close}。`,
    );
  }
  const label = cleanLabel(rawLabel, statement, contentStart);
  end = contentEnd + shape.close.length;
  return {
    node: { id, label: label || id, kind: shape.kind }, explicit: true, start: cursor, cursor: end,
  };
}

function parseEdge(statement: Statement, from: number): {
  cursor: number;
  label: string;
  kind: FlowchartEdge['kind'];
} {
  const text = statement.text;
  let cursor = from;
  let label = '';
  let kind: FlowchartEdge['kind'] = 'sequence';
  if (text.startsWith('-.->', cursor)) {
    cursor += 4;
    kind = 'exception';
  } else if (text.startsWith('-->', cursor)) {
    cursor += 3;
  } else if (text.startsWith('==>', cursor)) {
    cursor += 3;
  } else if (text.startsWith('--', cursor)) {
    const close = text.indexOf('-->', cursor + 2);
    if (close < 0) throw unsupportedEdge(statement, cursor);
    label = cleanLabel(text.slice(cursor + 2, close), statement, cursor + 2);
    cursor = close + 3;
  } else if (text.startsWith('-.', cursor)) {
    const close = text.indexOf('.->', cursor + 2);
    if (close < 0) throw unsupportedEdge(statement, cursor);
    label = cleanLabel(text.slice(cursor + 2, close), statement, cursor + 2);
    cursor = close + 3;
    kind = 'exception';
  } else if (text.startsWith('==', cursor)) {
    const close = text.indexOf('==>', cursor + 2);
    if (close < 0) throw unsupportedEdge(statement, cursor);
    label = cleanLabel(text.slice(cursor + 2, close), statement, cursor + 2);
    cursor = close + 3;
  } else {
    throw unsupportedEdge(statement, cursor);
  }
  cursor = skipWhitespace(text, cursor);
  if (text[cursor] === '|') {
    const close = text.indexOf('|', cursor + 1);
    if (close < 0) {
      throw syntaxAt(statement, cursor, '连线文字缺少结束竖线', '请使用 -->|条件| 的格式填写连线文字。');
    }
    label = cleanLabel(text.slice(cursor + 1, close), statement, cursor + 1);
    cursor = close + 1;
  }
  return { cursor, label, kind };
}

function unsupportedEdge(statement: Statement, cursor: number): MermaidImportError {
  return syntaxAt(
    statement, cursor, '不支持该连线语法',
    '请使用 -->、-->|文字|、-- 文字 -->、-.-> 或 ==> 连接节点。',
  );
}

function registerNode(
  nodes: Map<string, ParsedNode>,
  node: FlowchartNode,
  explicit: boolean,
  position: SourcePosition,
): void {
  const existing = nodes.get(node.id);
  if (!existing) {
    nodes.set(node.id, { ...node, explicit, position });
    if (nodes.size > MERMAID_IMPORT_MAX_NODES) {
      throw new MermaidImportError({
        code: 'MERMAID_NODE_LIMIT', category: '规模超限',
        message: `节点数量超过上限 ${MERMAID_IMPORT_MAX_NODES} 个`,
        suggestion: `请拆分流程，确保单次导入不超过 ${MERMAID_IMPORT_MAX_NODES} 个节点。`,
        ...position,
      });
    }
    return;
  }
  if (!explicit) return;
  if (existing.explicit) {
    throw new MermaidImportError({
      code: 'MERMAID_DUPLICATE_NODE_ID', category: 'Mermaid 语法错误',
      message: `节点 ID 重复：${node.id}`,
      suggestion: '请为每个显式节点使用唯一 ID；连线中可以直接重复引用已有 ID。',
      ...position,
    });
  }
  nodes.set(node.id, { ...node, explicit: true, position });
}

function inferTerminalKinds(nodes: Map<string, ParsedNode>, edges: FlowchartEdge[]): void {
  const incoming = new Map([...nodes.keys()].map((id) => [id, 0]));
  const outgoing = new Map([...nodes.keys()].map((id) => [id, 0]));
  for (const edge of edges) {
    outgoing.set(edge.from, (outgoing.get(edge.from) ?? 0) + 1);
    incoming.set(edge.to, (incoming.get(edge.to) ?? 0) + 1);
  }
  for (const node of nodes.values()) {
    if (node.kind !== 'action') continue;
    const before = incoming.get(node.id) ?? 0;
    const after = outgoing.get(node.id) ?? 0;
    if (before === 0 && after > 0) node.kind = 'start';
    else if (after === 0 && before > 0) node.kind = 'end';
  }
}

function requiredSource(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw diagnostic(
      'MERMAID_EMPTY', '内容错误', 'Mermaid 内容为空',
      '请粘贴 Mermaid 源码，或选择 .mmd/.mermaid 文件。',
    );
  }
  const bytes = Buffer.byteLength(value, 'utf8');
  if (bytes > MERMAID_IMPORT_MAX_BYTES) {
    throw diagnostic(
      'MERMAID_SOURCE_TOO_LARGE', '规模超限',
      `Mermaid 内容为 ${bytes} 字节，超过 1 MiB 上限`,
      '请拆分文件后分别导入，每个文件不超过 1 MiB。',
    );
  }
  if (CONTROL_CHAR.test(value)) {
    throw diagnostic(
      'MERMAID_CONTROL_CHARACTER', '内容错误', 'Mermaid 内容包含不支持的控制字符',
      '请将文件保存为 UTF-8 纯文本后重试。',
    );
  }
  return value;
}

function requiredTitle(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw diagnostic(
      'MERMAID_TITLE_REQUIRED', '流程名称错误', '请填写新流程名称',
      '流程名称用于流程列表和知识包流程文件，不能为空。',
    );
  }
  const title = value.normalize('NFC').trim();
  try {
    diagramFilePath(title);
  } catch (cause) {
    throw nameError(cause instanceof Error ? cause.message : '流程名称不能用于流程图文件名');
  }
  return title;
}

function optionalFilename(value: unknown): string | undefined {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'string') {
    throw diagnostic('MERMAID_FILENAME_INVALID', '内容错误', '文件名格式不正确', '请重新选择 Mermaid 文件。');
  }
  const filename = value.normalize('NFC').trim();
  if (!filename || filename.length > 255 || filename === '.' || filename === '..'
    || /[\/\\\u0000-\u001f\u007f]/u.test(filename)) {
    throw diagnostic('MERMAID_FILENAME_INVALID', '内容错误', '文件名格式不正确', '请重命名文件后重试。');
  }
  if (!SUPPORTED_FILE.test(filename)) {
    throw diagnostic(
      'MERMAID_FILE_TYPE_UNSUPPORTED', '内容错误', `不支持文件类型：${filename}`,
      '请选择扩展名为 .mmd 或 .mermaid 的 UTF-8 文本文件。',
    );
  }
  return filename;
}

function assertUniqueTitle(current: FlowchartDocument, title: string): void {
  const path = diagramFilePath(title).toLocaleLowerCase('en-US');
  const duplicate = current.diagrams.some((diagram) => (
    diagramFilePath(diagram.title).toLocaleLowerCase('en-US') === path
  ));
  if (duplicate) {
    throw diagnostic(
      'MERMAID_TITLE_DUPLICATE', '流程名称错误', `流程名称重复：“${title}”`,
      '请填写一个未使用的新流程名称；导入不会覆盖已有流程。',
    );
  }
}

function nameError(message: string): MermaidImportError {
  return diagnostic(
    'MERMAID_TITLE_INVALID', '流程名称错误', message,
    '请填写唯一且可用于文件名的流程名称，长度不超过 80 个字符。',
  );
}

function stableDiagramId(title: string, source: string): string {
  return `diagram-import-${createHash('sha256').update(title).update('\u0000').update(source).digest('hex').slice(0, 16)}`;
}

function splitStatements(rawLine: string, lineIndex: number): Statement[] {
  const statements: Statement[] = [];
  let start = 0;
  let quote = '';
  const stack: string[] = [];
  for (let index = 0; index < rawLine.length; index += 1) {
    const char = rawLine[index];
    if (quote) {
      if (char === quote && rawLine[index - 1] !== '\\') quote = '';
      continue;
    }
    if (char === '"' || char === "'") { quote = char; continue; }
    if (char === '%' && rawLine[index + 1] === '%' && !stack.length) {
      statements.push({ text: rawLine.slice(start, index), line: lineIndex + 1, column: start + 1, sourceLine: rawLine });
      return statements;
    }
    if ('[({'.includes(char)) stack.push(char);
    else if (']})'.includes(char) && stack.length) stack.pop();
    else if (char === ';' && !stack.length) {
      statements.push({ text: rawLine.slice(start, index), line: lineIndex + 1, column: start + 1, sourceLine: rawLine });
      start = index + 1;
    }
  }
  statements.push({ text: rawLine.slice(start), line: lineIndex + 1, column: start + 1, sourceLine: rawLine });
  return statements;
}

function findClosing(text: string, from: number, close: string): number {
  let quote = '';
  for (let index = from; index <= text.length - close.length; index += 1) {
    const char = text[index];
    if (quote) {
      if (char === quote && text[index - 1] !== '\\') quote = '';
      continue;
    }
    if (char === '"' || char === "'") { quote = char; continue; }
    if (text.startsWith(close, index)) return index;
  }
  return -1;
}

function cleanLabel(value: string, statement: Statement, offset: number): string {
  let label = value.trim();
  if ((label.startsWith('"') && label.endsWith('"')) || (label.startsWith("'") && label.endsWith("'"))) {
    label = label.slice(1, -1).replace(/\\([\\"'])/gu, '$1').trim();
  }
  if (/<\/?[A-Za-z][^>]*>/u.test(label) || /(?:https?|javascript|data)\s*:/iu.test(label)) {
    throw new MermaidImportError({
      code: 'MERMAID_HTML_FORBIDDEN', category: '安全限制',
      message: '节点或连线文字中不允许使用 HTML、脚本或外部跳转内容',
      suggestion: '请改用纯文本节点和连线文字。',
      ...positionAt(statement, offset),
    });
  }
  return label.replace(/<br\s*\/?\s*>/giu, ' ').replace(/\s+/gu, ' ').trim();
}

function skipWhitespace(text: string, from: number): number {
  let cursor = from;
  while (/\s/u.test(text[cursor] ?? '')) cursor += 1;
  return cursor;
}

function positionAt(statement: Statement, offset: number): SourcePosition {
  return {
    line: statement.line,
    column: statement.column + offset,
    sourceLine: statement.sourceLine,
  };
}

function syntaxAt(statement: Statement, offset: number, message: string, suggestion: string): MermaidImportError {
  return new MermaidImportError({
    code: 'MERMAID_SYNTAX_INVALID', category: 'Mermaid 语法错误', message, suggestion,
    ...positionAt(statement, offset),
  });
}

function statementError(
  code: string,
  category: MermaidImportErrorCategory,
  message: string,
  suggestion: string,
  statement: Statement,
): MermaidImportError {
  return new MermaidImportError({ code, category, message, suggestion, ...positionAt(statement, 0) });
}

function atLine(
  code: string,
  category: MermaidImportErrorCategory,
  message: string,
  suggestion: string,
  lineIndex: number,
  lines: string[],
  offset: number,
): MermaidImportError {
  return new MermaidImportError({
    code, category, message, suggestion,
    line: lineIndex + 1, column: Math.max(0, offset) + 1, sourceLine: lines[lineIndex],
  });
}

function diagnostic(
  code: string,
  category: MermaidImportErrorCategory,
  message: string,
  suggestion: string,
): MermaidImportError {
  return new MermaidImportError({ code, category, message, suggestion });
}
