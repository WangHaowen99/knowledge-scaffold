export const MERMAID_SOURCE_VALIDATOR_VERSION = 'mermaid-source-v1';

export interface MermaidSourceIssue {
  ruleId: 'mermaid.no-markdown-fence' | 'mermaid.flowchart-declaration';
  path: string;
  message: string;
  suggestion: string;
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
  return issues;
}
