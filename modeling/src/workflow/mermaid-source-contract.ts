export const MERMAID_SOURCE_VALIDATOR_VERSION = 'mermaid-source-v2';

export interface MermaidSourceIssue {
  ruleId: 'mermaid.no-markdown-fence' | 'mermaid.flowchart-declaration' | 'mermaid.label-quoting';
  path: string;
  message: string;
  suggestion: string;
}

// Mermaid 只在双引号标签里安全承载这些字符；未加引号时词法器会把它们当成形状或
// 连线分隔符，浏览器端直接抛 Parse error。实测 mermaid 11：`<br/>`、全角括号和
// 中文标点都合法，所以只拦这一组，不把能正常渲染的 A[开始] 一起判为违规。
const LABEL_STRUCTURE_CHARACTERS = /["()[\]{}|]/u;
const IDENTIFIER_CHARACTER = /[A-Za-z0-9_-]/u;
// 连字符既是节点 ID 的合法字符，又是 --> / -.-> 的箭头尾巴，所以 >…] 形状必须额外
// 排除它，否则 A[开始] --> B[结束] 里的 > 会被当成形状开头、跨过箭头吞掉后一个节点。
const ASYMMETRIC_SHAPE_PREFIX = /[A-Za-z0-9_]/u;
// 单个流程图的违规可能成百上千，findings 要回灌 Agent 上下文，必须封顶。
const MAX_LABEL_ISSUES = 10;
const LABEL_SNIPPET_LIMIT = 60;

// 长开闭符必须排在短的前面，否则 (( 会被 ( 抢先匹配、内容被截成半个形状。
const LABEL_SHAPES: ReadonlyArray<readonly [open: string, close: string]> = [
  ['(((', ')))'], ['([', '])'], ['[[', ']]'], ['[(', ')]'], ['((', '))'], ['{{', '}}'],
  ['[/', '/]'], ['[/', '\\]'], ['[\\', '\\]'], ['[\\', '/]'],
  ['[', ']'], ['(', ')'], ['{', '}'], ['>', ']'],
];

function findClosing(line: string, from: number, close: string): number {
  let index = from;
  while (index < line.length) {
    if (line[index] === '"') {
      // 引号内的 ] 和 } 是普通文字，Mermaid 不会当成收尾符，扫描也不能当。
      const quoteEnd = line.indexOf('"', index + 1);
      if (quoteEnd < 0) return -1;
      index = quoteEnd + 1;
      continue;
    }
    if (line.startsWith(close, index)) return index;
    index += 1;
  }
  return -1;
}

function matchShape(line: string, index: number): { contentStart: number; contentEnd: number; close: string } | undefined {
  for (const [open, close] of LABEL_SHAPES) {
    if (!line.startsWith(open, index)) continue;
    const contentStart = index + open.length;
    const contentEnd = findClosing(line, contentStart, close);
    if (contentEnd >= 0) return { contentStart, contentEnd, close };
  }
  return undefined;
}

/** 取出一行里的节点形状文字和 |…| 连线文字，不做完整语法解析。 */
function scanLabels(line: string): string[] {
  const labels: string[] = [];
  let index = 0;
  while (index < line.length) {
    const char = line[index]!;
    if (char === '"') {
      const quoteEnd = line.indexOf('"', index + 1);
      if (quoteEnd < 0) break;
      index = quoteEnd + 1;
      continue;
    }
    if (char === '|') {
      const labelEnd = findClosing(line, index + 1, '|');
      if (labelEnd < 0) break;
      labels.push(line.slice(index + 1, labelEnd));
      index = labelEnd + 1;
      continue;
    }
    // 形状符必须紧跟节点 ID，否则 --> 里的 > 会被当成 >…] 形状开头。
    const previous = index > 0 ? line[index - 1]! : '';
    const canOpenShape = IDENTIFIER_CHARACTER.test(previous)
      && (char !== '>' || ASYMMETRIC_SHAPE_PREFIX.test(previous));
    const shape = canOpenShape ? matchShape(line, index) : undefined;
    if (!shape) {
      index += 1;
      continue;
    }
    labels.push(line.slice(shape.contentStart, shape.contentEnd));
    index = shape.contentEnd + shape.close.length;
  }
  return labels;
}

function labelProblem(label: string): 'unquoted' | 'inner-quote' | undefined {
  const text = label.trim();
  if (!text) return undefined;
  if (text.startsWith('"')) {
    return text.indexOf('"', 1) === text.length - 1 ? undefined : 'inner-quote';
  }
  return LABEL_STRUCTURE_CHARACTERS.test(text) ? 'unquoted' : undefined;
}

function snippet(label: string): string {
  const text = label.trim();
  return text.length > LABEL_SNIPPET_LIMIT ? `${text.slice(0, LABEL_SNIPPET_LIMIT)}…` : text;
}

function labelIssues(path: string, lines: string[]): MermaidSourceIssue[] {
  const issues: MermaidSourceIssue[] = [];
  let total = 0;
  lines.forEach((line, offset) => {
    // Mermaid 渲染前整行删除 %% 注释，注释里的括号不会进语法分析。
    if (/^\s*%%/u.test(line)) return;
    for (const label of scanLabels(line)) {
      const problem = labelProblem(label);
      if (!problem) continue;
      total += 1;
      if (issues.length >= MAX_LABEL_ISSUES) continue;
      issues.push(problem === 'unquoted'
        ? {
          ruleId: 'mermaid.label-quoting',
          path,
          message: `${path} 第 ${offset + 1} 行的流程图文字含 Mermaid 结构字符 ( ) [ ] { } | "，未用双引号包裹会导致图形视图解析失败：${snippet(label)}`,
          suggestion: '把该文字整体用双引号包裹，例如 CHK{"X ≥ MIN(TURN, COMPANY)？"}、A -->|"超限(含税)"| B；保留业务原文，不要删改括号或替换为其他符号',
        }
        : {
          ruleId: 'mermaid.label-quoting',
          path,
          message: `${path} 第 ${offset + 1} 行的流程图文字在双引号内还有未转义的双引号，Mermaid 无法解析：${snippet(label)}`,
          suggestion: '双引号包裹的文字内部不能再出现双引号，改用中文引号「」或去掉内部引号',
        });
    }
  });
  if (total > issues.length) {
    issues.push({
      ruleId: 'mermaid.label-quoting',
      path,
      message: `${path} 还有 ${total - issues.length} 处同类流程图文字未按要求加引号，本次只列出前 ${MAX_LABEL_ISSUES} 处`,
      suggestion: '按同样方式给全文所有节点和连线文字补齐双引号后重新写入，校验会继续检查剩余位置',
    });
  }
  return issues;
}

export function validateMermaidSource(path: string, content: string): MermaidSourceIssue[] {
  if (!/^diagrams\/[^/]+\.mmd$/u.test(path)) return [];

  const normalized = content.replace(/^\uFEFF/u, '');
  const lines = normalized.split(/\r?\n/u);
  const issues: MermaidSourceIssue[] = [];
  if (lines.some((line) => /^\s*(?:`{3,}|~{3,})/u.test(line))) {
    issues.push({
      ruleId: 'mermaid.no-markdown-fence',
      path,
      message: `${path} 必须保存裸 Mermaid 源码，不能包含 Markdown 代码围栏`,
      suggestion: '删除开头的 ```mermaid 和结尾的 ``` 等代码围栏，只保留 Mermaid 源码正文',
    });
  }

  const declaration = lines
    .map((line) => line.trim())
    .find((line) => line && !line.startsWith('%%'));
  if (declaration !== 'flowchart TD' && declaration !== 'flowchart LR') {
    issues.push({
      ruleId: 'mermaid.flowchart-declaration',
      path,
      message: `${path} 的首个有效行必须是 flowchart TD 或 flowchart LR`,
      suggestion: '保留前置 %% 注释，并让首个非空、非注释行使用 flowchart TD 或 flowchart LR',
    });
  }
  issues.push(...labelIssues(path, lines));
  return issues;
}
