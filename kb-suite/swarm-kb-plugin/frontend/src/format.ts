import type { DocumentItem, Locator } from './types';

export function formatBytes(size: number | null | undefined): string {
  if (!size && size !== 0) return '—';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const STAGE_LABEL: Record<string, string> = {
  parse: '解析中',
  chunk: '切分中',
  embed: '向量化中',
};

export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

export function statusOf(doc: DocumentItem): { label: string; tone: Tone; busy: boolean } {
  switch (doc.parse_status) {
    case 'pending':
      return { label: '排队中', tone: 'neutral', busy: true };
    case 'processing':
      return { label: STAGE_LABEL[doc.stage ?? ''] ?? '处理中', tone: 'info', busy: true };
    case 'completed':
      return { label: '已完成', tone: 'success', busy: false };
    case 'failed':
      return { label: '失败', tone: 'danger', busy: false };
    case 'cancelled':
      return { label: '已取消', tone: 'warning', busy: false };
    case 'deleting':
      return { label: '删除中', tone: 'neutral', busy: true };
    default:
      return { label: doc.parse_status, tone: 'neutral', busy: false };
  }
}

export function summaryBusy(doc: DocumentItem): boolean {
  return doc.parse_status === 'completed' && (doc.summary_status === 'pending' || doc.summary_status === 'processing');
}

const RUNNING = new Set(['pending', 'processing']);

/** Summary, questions or wiki still running after parsing finished. */
export function enrichBusy(doc: DocumentItem): boolean {
  return doc.parse_status === 'completed' && [doc.summary_status, doc.question_status, doc.wiki_status].some((s) => RUNNING.has(s));
}

export const ENRICH_LABEL: Record<string, string> = {
  pending: '排队中',
  processing: '进行中',
  completed: '已完成',
  partial: '部分完成',
  failed: '失败',
  none: '未开启',
};

export function enrichItems(doc: DocumentItem): { key: string; label: string; status: string; tone: Tone }[] {
  const tone = (s: string): Tone => (s === 'failed' ? 'danger' : s === 'partial' ? 'warning' : RUNNING.has(s) ? 'info' : 'success');
  return [
    ['summary', '摘要', doc.summary_status],
    ['questions', '问题', doc.question_status],
    ['wiki', 'Wiki', doc.wiki_status],
  ]
    .filter(([, , s]) => s && s !== 'none')
    .map(([key, label, status]) => ({ key, label, status, tone: tone(status) }));
}

export function locatorText(locators: Locator[] | undefined): string {
  if (!locators?.length) return '';
  const parts: string[] = [];
  for (const loc of locators.slice(0, 3)) {
    if (loc.page) parts.push(`第 ${loc.page} 页`);
    else if (loc.slide) parts.push(`第 ${loc.slide} 张幻灯片`);
    else if (loc.sheet || loc.row_start) {
      const rows = loc.row_start ? ` 第 ${loc.row_start}–${loc.row_end ?? loc.row_start} 行` : '';
      parts.push(`${loc.sheet ?? '表格'}${rows}`);
    }
  }
  return Array.from(new Set(parts)).join('、');
}

export function sourceLabel(doc: DocumentItem): string {
  if (doc.source_type === 'url') return '网页';
  if (doc.source_type === 'text') return '文本';
  return (doc.file_type ?? '').toUpperCase() || '文件';
}
