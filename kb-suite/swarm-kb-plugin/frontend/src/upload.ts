import { rpc, RpcError } from './rpc';
import type { DocumentItem } from './types';

// the page's own cap; a backend behind kb-gateway may advertise its own (limits.max_upload_mb)
export const MAX_FILE_MB = 200;

export const ACCEPT =
  '.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.csv,.tsv,.md,.markdown,.txt,.json,.html,.htm,.epub';

function toBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return window.btoa(binary);
}

async function sha256Hex(file: File): Promise<string | undefined> {
  if (!window.isSecureContext || !window.crypto?.subtle) return undefined;
  const digest = await window.crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

export type ProcessOverrides = Record<string, Record<string, unknown>>;

export class UploadResultUnknown extends Error {}

export async function uploadFile(
  kbId: string,
  file: File,
  onProgress: (ratio: number) => void,
  overrides?: ProcessOverrides | null,
  maxMb = MAX_FILE_MB,
  folderPath?: string,
): Promise<DocumentItem> {
  if (file.size > maxMb * 1024 * 1024) throw new Error(`文件超过 ${maxMb}MB`);
  if (file.size === 0) throw new Error('文件为空');
  const sha256 = await sha256Hex(file);
  const begin = await rpc.call<{ upload_id: string; part_size: number; part_count: number }>('upload.begin', {
    kb_id: kbId,
    file_name: file.name,
    folder_path: folderPath,
    file_size: file.size,
    sha256,
    process_overrides: overrides ?? undefined,
  });
  for (let index = 0; index < begin.part_count; index += 1) {
    const start = index * begin.part_size;
    const blob = file.slice(start, Math.min(file.size, start + begin.part_size));
    const data = toBase64(await blob.arrayBuffer());
    await rpc.call('upload.part', { upload_id: begin.upload_id, index, data }, 300000);
    onProgress((index + 1) / begin.part_count);
  }
  try {
    return await rpc.call<DocumentItem>('upload.complete', { upload_id: begin.upload_id }, 300000);
  } catch (err) {
    if (err instanceof RpcError && (err.code === 'disconnected' || err.code === 'timeout' || err.status === 408 || err.status >= 500)) {
      throw new UploadResultUnknown(`结果待核对（${err.message}），请刷新文档列表后确认`);
    }
    throw err;
  }
}
