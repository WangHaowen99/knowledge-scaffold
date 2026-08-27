import type { DocumentRecord } from '../types.js';

export interface MaterialInventoryItem {
  id: string;
  filename: string;
  kind: DocumentRecord['kind'];
  parseStatus: DocumentRecord['parseStatus'];
  textLength: number;
  tableCount: number;
  purpose: NonNullable<DocumentRecord['purpose']>;
  readingMode: NonNullable<DocumentRecord['readingMode']>;
}

export function buildMaterialInventory(documents: DocumentRecord[]): MaterialInventoryItem[] {
  return documents.map((document) => ({
    id: document.id,
    filename: document.filename,
    kind: document.kind,
    parseStatus: document.parseStatus,
    textLength: document.textContent?.length ?? 0,
    tableCount: document.tables?.length ?? 0,
    purpose: document.purpose ?? 'auto',
    readingMode: document.readingMode ?? 'on_demand',
  }));
}

const kindLabels: Record<MaterialInventoryItem['kind'], string> = {
  markdown: 'Markdown 文档',
  text: '文本文档',
  pdf: 'PDF 文档',
  word: 'Word 文档',
  excel: 'Excel 表格',
  csv: 'CSV 表格',
  unknown: '文件',
};

const statusLabels: Record<MaterialInventoryItem['parseStatus'], string> = {
  pending: '解析中',
  parsed: '解析成功',
  failed: '解析失败',
};

export function materialInventoryItemSummary(item: MaterialInventoryItem): string {
  const filename = item.filename.replace(/\s+/gu, ' ').trim();
  const details = [kindLabels[item.kind], statusLabels[item.parseStatus]];
  if (item.tableCount > 0) details.push(`${item.tableCount} 张表`);
  if (item.textLength > 0) {
    // 保留旧的“【文件名】共 N 字符”前缀，兼容演示模型对文本文档的分页识别。
    return `【${filename}】共 ${item.textLength} 字符（${details.join('，')}）`;
  }
  return `【${filename}】（${details.join('，')}）`;
}

export function hasMaterialInventoryIntent(message: string): boolean {
  const normalized = String(message ?? '').replace(/\s+/gu, ' ').trim();
  if (!normalized) return false;
  if (/(?:如何|怎么|怎样|为什么|为何|原因|教程|方法|步骤)/u.test(normalized)) return false;
  const mentionsMaterial = /(?:文档|文件|资料|材料|附件)/u.test(normalized);
  const mentionsUpload = /(?:上传|已传|传上|接收|收到|导入)/u.test(normalized);
  const asksCountOrList = /(?:多少|几(?:个|份)?|哪些|哪(?:个|些|几份)?|清单|列表|数量)/u.test(normalized);
  const asksStatus = /(?:是否|有没有|有无).{0,12}(?:上传|已传|传上|接收|收到|导入)|(?:上传|已传|传上|接收|收到|导入).{0,12}(?:是否|有没有|有无|状态|情况|(?:成功|失败|完成)(?:了)?(?:吗|么|没有))|(?:都|全部).{0,6}(?:上传|已传|传上|接收|收到|导入).{0,6}(?:成功|失败|完成)/u
    .test(normalized);
  return mentionsMaterial && mentionsUpload && (asksCountOrList || asksStatus);
}

function numberedList(items: MaterialInventoryItem[]): string {
  return items.map((item, index) => `${index + 1}. ${materialInventoryItemSummary(item)}`).join('\n');
}

export function answerMaterialInventoryQuestion(
  message: string,
  materials: MaterialInventoryItem[] | undefined,
): string | undefined {
  if (!materials || !hasMaterialInventoryIntent(message)) return undefined;
  if (!materials.length) return '当前项目还没有接收到建模资料。';

  const parsed = materials.filter((item) => item.parseStatus === 'parsed');
  const pending = materials.filter((item) => item.parseStatus === 'pending');
  const failed = materials.filter((item) => item.parseStatus === 'failed');
  const summary = parsed.length === materials.length
    ? `当前项目共接收到 ${materials.length} 份资料，均已上传并解析成功。`
    : `当前项目共接收到 ${materials.length} 份资料：${parsed.length} 份解析成功、${pending.length} 份解析中、${failed.length} 份解析失败。`;
  const sections = [summary];
  if (parsed.length) sections.push(`上传并解析成功的资料：\n${numberedList(parsed)}`);
  if (pending.length) sections.push(`仍在解析的资料：\n${numberedList(pending)}`);
  if (failed.length) sections.push(`解析失败的资料：\n${numberedList(failed)}`);
  return sections.join('\n\n');
}
