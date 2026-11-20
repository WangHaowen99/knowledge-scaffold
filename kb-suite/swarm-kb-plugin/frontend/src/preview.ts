// Original-file preview and evidence location: pure decisions, no imports (node tests load this file directly).

export type PreviewKind = 'pdf' | 'image' | 'audio' | 'video' | 'docx' | 'none';

/** Files above this are offered for download only; nothing bigger is assembled in the page. */
export const MAX_PREVIEW_BYTES = 200 * 1024 * 1024;
/** One `docs.file_range` call. */
export const RANGE_BYTES = 8 * 1024 * 1024;

const BY_EXT: Record<string, PreviewKind> = {
  pdf: 'pdf',
  docx: 'docx',
  png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', webp: 'image', bmp: 'image', svg: 'image', avif: 'image',
  mp3: 'audio', wav: 'audio', m4a: 'audio', aac: 'audio', ogg: 'audio', oga: 'audio', flac: 'audio', opus: 'audio',
  mp4: 'video', webm: 'video', mov: 'video', m4v: 'video', ogv: 'video',
};

/** How a file can be shown, from its extension, or its content type when the extension says nothing. */
export function previewKind(fileType: string | null | undefined, contentType?: string | null): PreviewKind {
  const ext = (fileType ?? '').toLowerCase().replace(/^\./, '');
  if (BY_EXT[ext]) return BY_EXT[ext];
  const ct = (contentType ?? '').toLowerCase().split(';')[0].trim();
  if (ct === 'application/pdf') return 'pdf';
  if (ct === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') return 'docx';
  for (const kind of ['image', 'audio', 'video'] as const) if (ct.startsWith(`${kind}/`)) return kind;
  return 'none';
}

/** The MIME type to give a Blob of this kind when the backend only says application/octet-stream. */
export function blobType(fileType: string | null | undefined, contentType: string | null | undefined): string {
  const ct = (contentType ?? '').split(';')[0].trim();
  if (ct && ct !== 'application/octet-stream') return ct;
  const ext = (fileType ?? '').toLowerCase();
  const known: Record<string, string> = {
    pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp',
    bmp: 'image/bmp', svg: 'image/svg+xml', avif: 'image/avif', mp3: 'audio/mpeg', wav: 'audio/wav', m4a: 'audio/mp4',
    aac: 'audio/aac', ogg: 'audio/ogg', oga: 'audio/ogg', flac: 'audio/flac', opus: 'audio/opus', mp4: 'video/mp4',
    webm: 'video/webm', mov: 'video/quicktime', m4v: 'video/mp4', ogv: 'video/ogg',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  };
  return known[ext] ?? 'application/octet-stream';
}

/** `[begin, end)` split into calls of at most `size` bytes. */
export function rangeCalls(begin: number, end: number, size = RANGE_BYTES): { offset: number; length: number }[] {
  const out: { offset: number; length: number }[] = [];
  for (let offset = begin; offset < end; offset += size) out.push({ offset, length: Math.min(size, end - offset) });
  return out;
}

/** CSS box of a PDF-point bbox `[x0, y0, x1, y1]` (origin top-left) on a page drawn at `scale`. */
export function bboxStyle(bbox: number[] | null | undefined, scale: number): { left: number; top: number; width: number; height: number } | null {
  if (!bbox || bbox.length !== 4 || bbox.some((v) => typeof v !== 'number' || !Number.isFinite(v))) return null;
  const [x0, y0, x1, y1] = bbox;
  return {
    left: Math.min(x0, x1) * scale,
    top: Math.min(y0, y1) * scale,
    width: Math.abs(x1 - x0) * scale,
    height: Math.abs(y1 - y0) * scale,
  };
}

/** Where to take the reader: a PDF page (and box), a media time, or lines / a passage of the text. */
export interface Focus {
  page?: number;
  bbox?: number[];
  time?: number;
  lineStart?: number;
  lineEnd?: number;
  text?: string;
}

type Locator = { page?: number | null; bbox?: number[] | null; line_start?: number | null; line_end?: number | null; time_start?: number | null };

/** A focus from evidence (or a hit's own locators): the first locator that says something, plus the line range. */
export function focusOf(source: {
  locators?: Locator[] | null;
  line_start?: number | null;
  line_end?: number | null;
  text?: string | null;
}): Focus {
  const focus: Focus = {};
  for (const loc of source.locators ?? []) {
    if (focus.page === undefined && typeof loc.page === 'number' && loc.page > 0) {
      focus.page = loc.page;
      if (Array.isArray(loc.bbox)) focus.bbox = loc.bbox;
    }
    if (focus.time === undefined && typeof loc.time_start === 'number') focus.time = loc.time_start;
    if (focus.lineStart === undefined && typeof loc.line_start === 'number') {
      focus.lineStart = loc.line_start;
      if (typeof loc.line_end === 'number') focus.lineEnd = loc.line_end;
    }
  }
  if (typeof source.line_start === 'number') {
    focus.lineStart = source.line_start;
    focus.lineEnd = typeof source.line_end === 'number' ? source.line_end : focus.lineEnd;
  }
  if (source.text?.trim()) focus.text = source.text.trim();
  return focus;
}

/** Which drawer view shows a focus best for a file of `kind`; `file` only when the original can be shown. */
export function focusView(focus: Focus, kind: PreviewKind, canShowFile: boolean): 'file' | 'content' {
  if (canShowFile && kind === 'pdf' && focus.page !== undefined) return 'file';
  if (canShowFile && (kind === 'audio' || kind === 'video') && focus.time !== undefined) return 'file';
  return 'content';
}

/** 1-based line range of the first occurrence of `passage` in `text` (whitespace-insensitive), or null. */
export function linesOf(text: string, passage: string): { from: number; to: number } | null {
  const probe = passage.replace(/\s+/g, ' ').trim().slice(0, 80);
  if (!probe) return null;
  // match on a whitespace-collapsed copy, keeping where each collapsed character came from
  const map: number[] = [];
  let flat = '';
  for (let i = 0; i < text.length; i += 1) {
    const ws = /\s/.test(text[i]);
    if (ws && flat.endsWith(' ')) continue;
    flat += ws ? ' ' : text[i];
    map.push(i);
  }
  const at = flat.indexOf(probe);
  if (at < 0) return null;
  const start = map[at];
  const end = map[Math.min(at + passage.replace(/\s+/g, ' ').trim().length, flat.length) - 1];
  const lineAt = (i: number) => text.slice(0, i).split('\n').length;
  return { from: lineAt(start), to: lineAt(end) };
}

/**
 * The part of a long text to render so that `line` is inside: at most `limit` characters, cut at line
 * boundaries; `firstLine` is the 1-based number of its first line in the whole text.
 */
export function textWindow(text: string, limit: number, line?: number): { text: string; firstLine: number; truncated: boolean } {
  if (text.length <= limit) return { text, firstLine: 1, truncated: false };
  let start = 0;
  if (line !== undefined && line > 1) {
    let at = 0;
    for (let n = 1; n < line && at >= 0; n += 1) at = text.indexOf('\n', at) + 1 || -1;
    const target = at < 0 ? text.length : at;
    if (target > limit / 3) {
      start = text.lastIndexOf('\n', Math.max(0, target - Math.floor(limit / 3))) + 1;
    }
  }
  let end = Math.min(text.length, start + limit);
  if (end < text.length) end = text.lastIndexOf('\n', end) > start ? text.lastIndexOf('\n', end) : end;
  return { text: text.slice(start, end), firstLine: text.slice(0, start).split('\n').length, truncated: true };
}
