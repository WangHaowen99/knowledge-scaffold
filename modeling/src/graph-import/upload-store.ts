import { createHash, randomUUID } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { extname, join } from 'node:path';
import { config } from '../config.js';
import { getDb, nowIso } from '../db/index.js';

const DEFAULT_TTL_MS = 30 * 60_000;

export interface GraphImportUpload {
  id: string;
  projectId: string;
  owner: string;
  filename: string;
  storedPath: string;
  fileHash: string;
  sizeBytes: number;
  expiresAt: string;
  createdAt: string;
}

function rowToUpload(row: any): GraphImportUpload {
  return {
    id: row.id,
    projectId: row.project_id,
    owner: row.owner,
    filename: row.filename,
    storedPath: row.stored_path,
    fileHash: row.file_hash,
    sizeBytes: row.size_bytes,
    expiresAt: row.expires_at,
    createdAt: row.created_at,
  };
}

function removeFile(path: string): void {
  try { if (existsSync(path)) unlinkSync(path); } catch { /* cleanup is best-effort */ }
}

export function cleanupExpiredGraphImportUploads(now = new Date()): number {
  const rows = getDb().prepare('SELECT * FROM graph_import_uploads WHERE expires_at <= ?')
    .all(now.toISOString()) as any[];
  for (const row of rows) removeFile(row.stored_path);
  if (rows.length) {
    getDb().prepare('DELETE FROM graph_import_uploads WHERE expires_at <= ?').run(now.toISOString());
  }
  return rows.length;
}

export function saveGraphImportUpload(input: {
  projectId: string;
  owner: string;
  filename: string;
  buffer: Buffer;
  ttlMs?: number;
}): GraphImportUpload {
  cleanupExpiredGraphImportUploads();
  const extension = extname(input.filename).toLowerCase();
  if (!['.xlsx', '.xls'].includes(extension)) throw new Error('仅支持 .xlsx 或 .xls 文件');
  const id = `giu_${randomUUID().slice(0, 16)}`;
  const directory = join(config.dataDir, 'tmp', 'graph-import');
  mkdirSync(directory, { recursive: true });
  const storedPath = join(directory, `${id}${extension}`);
  const temporaryPath = `${storedPath}.tmp`;
  writeFileSync(temporaryPath, input.buffer);
  renameSync(temporaryPath, storedPath);
  const createdAt = nowIso();
  const expiresAt = new Date(Date.now() + (input.ttlMs ?? DEFAULT_TTL_MS)).toISOString();
  const fileHash = createHash('sha256').update(input.buffer).digest('hex');
  getDb().prepare(`INSERT INTO graph_import_uploads
      (id, project_id, owner, filename, stored_path, file_hash, size_bytes, expires_at, created_at)
    VALUES (?,?,?,?,?,?,?,?,?)`)
    .run(id, input.projectId, input.owner, input.filename, storedPath, fileHash,
      input.buffer.byteLength, expiresAt, createdAt);
  return {
    id,
    projectId: input.projectId,
    owner: input.owner,
    filename: input.filename,
    storedPath,
    fileHash,
    sizeBytes: input.buffer.byteLength,
    expiresAt,
    createdAt,
  };
}

export function getGraphImportUpload(input: {
  uploadId: string;
  projectId: string;
  owner: string;
}): GraphImportUpload {
  cleanupExpiredGraphImportUploads();
  const row = getDb().prepare(`SELECT * FROM graph_import_uploads
    WHERE id = ? AND project_id = ? AND owner = ?`)
    .get(input.uploadId, input.projectId, input.owner);
  if (!row) throw new Error('图导入临时文件不存在或已过期');
  const upload = rowToUpload(row);
  if (!existsSync(upload.storedPath)) {
    getDb().prepare('DELETE FROM graph_import_uploads WHERE id = ?').run(upload.id);
    throw new Error('图导入临时文件不存在或已过期');
  }
  return upload;
}

export function readGraphImportUpload(input: {
  uploadId: string;
  projectId: string;
  owner: string;
}): { upload: GraphImportUpload; buffer: Buffer } {
  const upload = getGraphImportUpload(input);
  const buffer = readFileSync(upload.storedPath);
  const hash = createHash('sha256').update(buffer).digest('hex');
  if (hash !== upload.fileHash) throw new Error('图导入文件校验失败，请重新上传');
  return { upload, buffer };
}

export function deleteGraphImportUpload(input: {
  uploadId: string;
  projectId: string;
  owner: string;
}): void {
  const row = getDb().prepare(`SELECT * FROM graph_import_uploads
    WHERE id = ? AND project_id = ? AND owner = ?`)
    .get(input.uploadId, input.projectId, input.owner) as any;
  if (!row) return;
  removeFile(row.stored_path);
  getDb().prepare('DELETE FROM graph_import_uploads WHERE id = ?').run(input.uploadId);
}

export function deleteProjectGraphImportUploadFiles(input: {
  projectId: string;
  owner: string;
}): number {
  const rows = getDb().prepare(`SELECT stored_path FROM graph_import_uploads
    WHERE project_id = ? AND owner = ?`).all(input.projectId, input.owner) as Array<{ stored_path: string }>;
  try {
    for (const row of rows) {
      if (existsSync(row.stored_path)) unlinkSync(row.stored_path);
    }
  } catch {
    throw new Error('图导入临时文件清理失败，请稍后重试');
  }
  return rows.length;
}
