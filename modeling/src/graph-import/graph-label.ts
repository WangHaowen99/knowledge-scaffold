const INVALID_GRAPH_LABEL = /[`\u0000-\u001f\u007f]/u;

export function assertGraphLabel(value: string, label: string): void {
  if (!value.trim()) throw new Error(`${label}不能为空`);
  if (INVALID_GRAPH_LABEL.test(value)) throw new Error(`${label}格式非法：${value}`);
}

export function quoteGraphLabel(value: string, label: string): string {
  const normalized = value.trim();
  assertGraphLabel(normalized, label);
  return `\`${normalized}\``;
}
