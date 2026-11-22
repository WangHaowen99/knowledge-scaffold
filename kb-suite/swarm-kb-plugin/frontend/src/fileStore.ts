// Original files for preview, read through `docs.file_range` (at most RANGE_BYTES per call).
// One file is held at a time: opening another, or closing the drawer, releases it (Blob URL revoked,
// PDF document destroyed, an unfinished read stopped).
import { MAX_PREVIEW_BYTES, RANGE_BYTES, blobType, rangeCalls } from './preview';
import { RpcError, rpc } from './rpc';

type Part = { content_type: string; offset: number; total: number | null; data: string };

export type Progress = (read: number, total: number | null) => void;

const decode = (b64: string) => Uint8Array.from(window.atob(b64), (c) => c.charCodeAt(0));

function concat(parts: Uint8Array[], size: number): Uint8Array {
  if (parts.length === 1) return parts[0];
  const out = new Uint8Array(size);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

export const tooLarge = () => new RpcError('too_large', '文件过大，无法预览');

async function call(docId: string, offset: number, length: number): Promise<Part> {
  return rpc.call<Part>('docs.file_range', { doc_id: docId, offset, length }, 300000);
}

/** Bytes `[begin, end)` of the original; `total` is the file size when the backend says it. */
export async function readRange(docId: string, begin: number, end: number): Promise<{ bytes: Uint8Array; total: number | null; contentType: string }> {
  const parts: Uint8Array[] = [];
  let size = 0;
  let total: number | null = null;
  let contentType = 'application/octet-stream';
  for (const { offset, length } of rangeCalls(begin, end)) {
    const res = await call(docId, offset, length);
    const bytes = decode(res.data);
    parts.push(bytes);
    size += bytes.length;
    total = res.total;
    contentType = res.content_type;
    if (bytes.length < length) break;
  }
  return { bytes: concat(parts, size), total, contentType };
}

/** The whole original, part by part; refused above MAX_PREVIEW_BYTES. */
async function readAll(docId: string, stop: { cancelled: boolean }, progress: { fn?: Progress }) {
  const parts: Uint8Array[] = [];
  let size = 0;
  let total: number | null = null;
  let contentType = 'application/octet-stream';
  for (;;) {
    if (stop.cancelled) throw new RpcError('cancelled', '已取消');
    const res = await call(docId, size, RANGE_BYTES);
    const bytes = decode(res.data);
    total = res.total ?? total;
    contentType = res.content_type;
    if ((total ?? 0) > MAX_PREVIEW_BYTES || size + bytes.length > MAX_PREVIEW_BYTES) throw tooLarge();
    parts.push(bytes);
    size += bytes.length;
    progress.fn?.(size, total);
    if (bytes.length === 0 || (total !== null ? size >= total : bytes.length < RANGE_BYTES)) break;
  }
  return { parts, size, total: total ?? size, contentType };
}

type Slot = { key: string; value: Promise<unknown>; stop: { cancelled: boolean }; progress: { fn?: Progress }; dispose: () => void };
let slot: Slot | null = null;

/** The value held under `key`, opening it (and releasing whatever else is held) when it is not the one held. */
export function hold<T>(
  key: string,
  open: (stop: { cancelled: boolean }, progress: { fn?: Progress }) => Promise<T>,
  dispose: (value: T) => void,
  onProgress?: Progress,
): Promise<T> {
  if (slot?.key === key) {
    slot.progress.fn = onProgress;
    return slot.value as Promise<T>;
  }
  releaseFile();
  const stop = { cancelled: false };
  const progress = { fn: onProgress };
  const value = open(stop, progress);
  const held: Slot = {
    key,
    value,
    stop,
    progress,
    dispose: () => {
      stop.cancelled = true;
      value.then(dispose, () => undefined);
    },
  };
  slot = held;
  value.catch(() => {
    if (slot === held) slot = null;
  });
  return value;
}

/** Release the held file (only if it belongs to `docId`, when given). */
export function releaseFile(docId?: string): void {
  if (!slot || (docId && !slot.key.startsWith(`${docId}|`))) return;
  slot.dispose();
  slot = null;
}

export interface LoadedFile {
  url: string;
  blob: Blob;
  size: number;
}

/** The original as a Blob URL (images, audio, video, docx). */
export function loadBlob(docId: string, fileType: string | null, onProgress?: Progress): Promise<LoadedFile> {
  return hold(
    `${docId}|blob`,
    async (stop, progress) => {
      const { parts, size, contentType } = await readAll(docId, stop, progress);
      const blob = new Blob(parts as BlobPart[], { type: blobType(fileType, contentType) });
      return { url: URL.createObjectURL(blob), blob, size };
    },
    (file) => URL.revokeObjectURL(file.url),
    onProgress,
  );
}

/** The original as one byte array (PDF from a backend without range reads). */
export async function readWhole(docId: string, stop: { cancelled: boolean }, progress: { fn?: Progress }): Promise<Uint8Array> {
  const { parts, size } = await readAll(docId, stop, progress);
  return concat(parts, size);
}
