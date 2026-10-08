import { createHash, randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  writeFile,
} from 'node:fs/promises';
import { basename, dirname, extname, join, relative, resolve, sep } from 'node:path';
import type { DatabaseSync } from 'node:sqlite';

import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import { IsomorphicGitArtifactRepository } from '../artifacts/isomorphic-git-repository.js';

const TRANSFER_FORMAT = 'knowledge-scaffold-project-transfer';
const TRANSFER_VERSION = 1;
const SOURCE_PROJECT_ID = 'pj_source';
const SOURCE_OWNER = '__project_owner__';
const MANIFEST_PATH = 'manifest.json';
const DATABASE_PATH = 'database.json';
const AGENT_RUNTIME_PATH = 'agent-runtime.json';
const MAX_ARCHIVE_BYTES = 512 * 1024 * 1024;
const MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_ARCHIVE_ENTRIES = 100_000;
const QUERY_TUPLE_BATCH_SIZE = 80;
const GRAPH_UPLOAD_TTL_MS = 30 * 60_000;

const EXCLUDED_TABLES = new Set([
  'project_members',
  'project_mcp_endpoints',
  'project_mcp_tokens',
  'project_data_sources',
  'project_operation_leases',
  'inflight_operations',
  'modeling_mcp_runtime',
  'agent_run_cancellations',
  'user_llm_settings',
  'user_task_llm_settings',
  'llm_default_settings',
  'user_llm_default_snapshots',
]);

const ACCOUNT_COLUMNS = new Set([
  'owner',
  'username',
  'added_by',
  'actor_username',
  'subject_username',
  'issued_by',
  'revoked_by',
  'uploaded_by',
  'created_by',
  'updated_by',
  'override_actor',
  'resolution_actor',
  'accepted_by',
  'release_confirmed_by',
  'deferred_by',
  'released_by',
]);

type SqlValue = null | string | number | bigint | Uint8Array;
type SqlRow = Record<string, SqlValue>;

interface ColumnInfo {
  name: string;
  type: string;
  notnull: number;
  dflt_value: SqlValue;
  pk: number;
}

interface ForeignKeyColumn {
  from: string;
  to: string;
}

interface ForeignKeyInfo {
  id: number;
  table: string;
  columns: ForeignKeyColumn[];
}

interface TableInfo {
  name: string;
  columns: ColumnInfo[];
  primaryKey: string[];
  foreignKeys: ForeignKeyInfo[];
}

interface TransferFile {
  path: string;
  size: number;
  sha256: string;
}

interface GraphUploadFile {
  uploadId: string;
  path: string;
}

interface TransferManifest {
  format: typeof TRANSFER_FORMAT;
  version: typeof TRANSFER_VERSION;
  createdAt: string;
  project: {
    name: string;
    kind: string;
    runtime: string;
  };
  database: {
    path: typeof DATABASE_PATH;
    rowCount: number;
    tables: string[];
  };
  files: TransferFile[];
  graphUploads: GraphUploadFile[];
  agentRuntime?: {
    path: typeof AGENT_RUNTIME_PATH;
  };
  excluded: string[];
}

interface TransferDatabase {
  sourceProjectId: typeof SOURCE_PROJECT_ID;
  tables: Record<string, Array<Record<string, unknown>>>;
}

export interface ProjectTransferProgress {
  percent: number;
  message: string;
}

export interface BuildProjectTransferOptions {
  db: DatabaseSync;
  dataDir: string;
  projectId: string;
  owner: string;
  agentRuntime?: unknown;
  onProgress?: (progress: ProjectTransferProgress) => void;
}

export interface ImportProjectTransferOptions {
  db: DatabaseSync;
  dataDir: string;
  archive: Uint8Array;
  owner: string;
  /** 指定导入后使用的项目名称；为空时沿用迁移包内的原名称。 */
  projectName?: string;
  importAgentRuntime?: (
    projectId: string,
    payload: Record<string, unknown>,
  ) => Promise<Record<string, string> | void>;
  removeAgentRuntime?: (projectId: string) => Promise<void>;
  onProgress?: (progress: ProjectTransferProgress) => void;
}

export interface ImportedProjectTransfer {
  projectId: string;
  name: string;
  excluded: string[];
}

export class ProjectTransferError extends Error {
  constructor(
    message: string,
    public readonly code = 'invalid_project_transfer',
  ) {
    super(message);
    this.name = 'ProjectTransferError';
  }
}

export async function buildProjectTransferArchive(
  options: BuildProjectTransferOptions,
): Promise<Uint8Array> {
  const { db, dataDir, projectId, owner, onProgress } = options;
  const project = db.prepare('SELECT * FROM projects WHERE id = ? AND owner = ?')
    .get(projectId, owner) as SqlRow | undefined;
  if (!project) throw new ProjectTransferError('项目不存在或当前账号不是项目所有者', 'project_not_found');

  onProgress?.({ percent: 8, message: '正在收集项目数据' });
  const schemas = readTableInfo(db);
  assertProjectExportIdle(db, schemas, projectId);
  const rows = collectProjectRows(db, schemas, project);
  const graphUploadFiles = await collectGraphUploadFiles(rows.get('graph_import_uploads') ?? []);
  const normalizedTables = normalizeExportRows(rows, schemas, projectId);
  const database: TransferDatabase = {
    sourceProjectId: SOURCE_PROJECT_ID,
    tables: Object.fromEntries([...normalizedTables.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([table, tableRows]) => [table, tableRows.map(encodeRow)])),
  };
  const databaseBytes = strToU8(JSON.stringify(database));

  onProgress?.({ percent: 42, message: '正在收集项目文件与版本历史' });
  const archiveEntries: Record<string, Uint8Array> = { [DATABASE_PATH]: databaseBytes };
  if (options.agentRuntime !== undefined) {
    archiveEntries[AGENT_RUNTIME_PATH] = strToU8(JSON.stringify(options.agentRuntime));
  }
  const graphUploads: GraphUploadFile[] = [];
  for (const [index, upload] of graphUploadFiles.entries()) {
    const path = `graph-uploads/${String(index + 1).padStart(6, '0')}.bin`;
    archiveEntries[path] = upload.content;
    graphUploads.push({ uploadId: normalizeProjectText(upload.uploadId, projectId), path });
  }

  const artifactRoot = join(resolve(dataDir), 'artifacts', projectId);
  if (existsSync(artifactRoot)) {
    for (const file of await readDirectoryFiles(artifactRoot)) {
      archiveEntries[`artifacts/${file.path}`] = file.content;
    }
  }

  onProgress?.({ percent: 72, message: '正在生成迁移包校验清单' });
  const projectRow = normalizedTables.get('projects')?.[0];
  const files = Object.entries(archiveEntries)
    .map(([path, content]) => ({ path, size: content.byteLength, sha256: sha256(content) }))
    .sort((left, right) => left.path.localeCompare(right.path));
  const manifest: TransferManifest = {
    format: TRANSFER_FORMAT,
    version: TRANSFER_VERSION,
    createdAt: new Date().toISOString(),
    project: {
      name: String(projectRow?.name ?? 'project'),
      kind: String(projectRow?.kind ?? 'modeling'),
      runtime: String(projectRow?.runtime ?? 'legacy'),
    },
    database: {
      path: DATABASE_PATH,
      rowCount: [...normalizedTables.values()].reduce((total, tableRows) => total + tableRows.length, 0),
      tables: [...normalizedTables.keys()].sort(),
    },
    files,
    graphUploads,
    ...(options.agentRuntime !== undefined ? { agentRuntime: { path: AGENT_RUNTIME_PATH } } : {}),
    excluded: [
      ...EXCLUDED_TABLES,
      'agent_candidate_workspaces.credential_hash',
      'agent_candidate_workspaces.credential_expires_at',
    ].sort(),
  };
  archiveEntries[MANIFEST_PATH] = strToU8(JSON.stringify(manifest, null, 2));

  onProgress?.({ percent: 88, message: '正在压缩项目迁移包' });
  const archive = zipSync(archiveEntries, { level: 6 });
  onProgress?.({ percent: 100, message: '项目迁移包已生成' });
  return archive;
}

/**
 * 只解压 manifest.json 读取迁移包内的项目名称，用于导入前的重名校验。
 */
export function readProjectTransferName(archive: Uint8Array): string {
  if (archive.byteLength > MAX_ARCHIVE_BYTES) throw new ProjectTransferError('项目迁移包超过大小限制');
  inspectZipDirectory(archive);
  let entries: Record<string, Uint8Array>;
  try {
    entries = unzipSync(archive, { filter: (file) => file.name === MANIFEST_PATH });
  } catch {
    throw new ProjectTransferError('项目迁移包无法解压或已经损坏');
  }
  const name = parseManifest(entries[MANIFEST_PATH]).project.name.trim();
  if (!name) throw new ProjectTransferError('迁移包缺少项目名称');
  return name;
}

export async function importProjectTransferArchive(
  options: ImportProjectTransferOptions,
): Promise<ImportedProjectTransfer> {
  const { db, dataDir, archive, owner, onProgress } = options;
  if (!owner.trim()) throw new ProjectTransferError('导入账号不正确');
  if (archive.byteLength > MAX_ARCHIVE_BYTES) throw new ProjectTransferError('项目迁移包超过大小限制');

  onProgress?.({ percent: 8, message: '正在检查迁移包结构' });
  inspectZipDirectory(archive);
  let entries: Record<string, Uint8Array>;
  try {
    entries = unzipSync(archive);
  } catch {
    throw new ProjectTransferError('项目迁移包无法解压或已经损坏');
  }
  const manifest = parseManifest(entries[MANIFEST_PATH]);
  validateArchiveEntries(entries, manifest);

  onProgress?.({ percent: 24, message: '正在校验迁移包完整性' });
  for (const file of manifest.files) {
    const content = entries[file.path];
    if (!content || content.byteLength !== file.size || sha256(content) !== file.sha256) {
      throw new ProjectTransferError(`迁移包文件校验失败：${file.path}`);
    }
  }

  const database = parseTransferDatabase(entries[manifest.database.path]);
  const agentRuntime = parseAgentRuntime(entries, manifest);
  if (agentRuntime && !options.importAgentRuntime) {
    throw new ProjectTransferError('目标环境不支持导入建模助手数据');
  }
  const schemas = readTableInfo(db);
  const tables = decodeAndValidateTables(database, manifest, schemas);
  validateProjectClosure(tables, schemas);
  const projectRow = tables.get('projects')![0];
  // 改名必须发生在行转换之前，保证写库行与返回值使用同一个名称
  const requestedName = String(options.projectName ?? '').trim();
  if (requestedName) projectRow.name = requestedName;
  const newProjectId = createProjectId();
  const idMap = buildIdMap(tables, schemas, newProjectId);
  const transformed = transformImportRows(tables, schemas, idMap, owner, newProjectId);

  onProgress?.({ percent: 45, message: '正在准备项目文件' });
  const resolvedDataDir = resolve(dataDir);
  const temporaryRoot = join(resolvedDataDir, 'tmp');
  await mkdir(temporaryRoot, { recursive: true });
  const stagingRoot = await mkdtemp(join(temporaryRoot, 'project-transfer-'));
  const stagedArtifactDir = join(stagingRoot, newProjectId);
  const finalArtifactDir = join(resolvedDataDir, 'artifacts', newProjectId);
  const movedFiles: string[] = [];
  let artifactMoved = false;
  let agentImported = false;

  try {
    const artifactEntries = Object.entries(entries)
      .filter(([path]) => path.startsWith('artifacts/'));
    for (const [path, content] of artifactEntries) {
      const target = safeArchiveTarget(stagedArtifactDir, path.slice('artifacts/'.length));
      await mkdir(dirname(target), { recursive: true });
      await writeFile(target, content, { mode: 0o600 });
    }
    await validateArtifactBundle(transformed, artifactEntries.length, stagedArtifactDir);

    const graphTargets = prepareGraphUploadTargets({
      manifest,
      entries,
      tables: transformed,
      dataDir: resolvedDataDir,
      idMap,
    });
    for (const target of graphTargets) {
      const stagedPath = join(stagingRoot, 'graph-uploads', basename(target.finalPath));
      await mkdir(dirname(stagedPath), { recursive: true });
      await writeFile(stagedPath, target.content, { mode: 0o600 });
      target.stagedPath = stagedPath;
    }

    if (agentRuntime) {
      onProgress?.({ percent: 58, message: '正在恢复建模助手记录' });
      const agentIdMap = await options.importAgentRuntime!(newProjectId, agentRuntime);
      if (agentIdMap) applyImportedIdMap(transformed, agentIdMap, newProjectId);
      agentImported = true;
    }

    onProgress?.({ percent: 66, message: '正在恢复项目数据' });
    db.exec('BEGIN IMMEDIATE');
    try {
      db.exec('PRAGMA defer_foreign_keys = ON');
      insertTransferRows(db, transformed, schemas);

      if (artifactEntries.length) {
        if (existsSync(finalArtifactDir)) throw new ProjectTransferError('目标项目版本目录已经存在');
        await mkdir(dirname(finalArtifactDir), { recursive: true });
        await rename(stagedArtifactDir, finalArtifactDir);
        artifactMoved = true;
      }
      for (const target of graphTargets) {
        await mkdir(dirname(target.finalPath), { recursive: true });
        if (existsSync(target.finalPath)) throw new ProjectTransferError('目标项目上传文件已经存在');
        await rename(target.stagedPath!, target.finalPath);
        movedFiles.push(target.finalPath);
      }
      db.exec('COMMIT');
    } catch (error) {
      if (db.isTransaction) db.exec('ROLLBACK');
      throw error;
    }
  } catch (error) {
    for (const path of movedFiles) await rm(path, { force: true }).catch(() => undefined);
    if (artifactMoved) await rm(finalArtifactDir, { recursive: true, force: true }).catch(() => undefined);
    if (agentImported && options.removeAgentRuntime) {
      await options.removeAgentRuntime(newProjectId).catch(() => undefined);
    }
    if (error instanceof ProjectTransferError) throw error;
    const message = error instanceof Error ? error.message : String(error);
    throw new ProjectTransferError(`项目导入失败：${message}`);
  } finally {
    await rm(stagingRoot, { recursive: true, force: true }).catch(() => undefined);
  }

  onProgress?.({ percent: 100, message: '项目导入完成' });
  return {
    projectId: newProjectId,
    name: String(projectRow.name ?? manifest.project.name),
    excluded: manifest.excluded,
  };
}

export function projectTransferFilename(name: string): string {
  const normalized = String(name || 'project')
    .normalize('NFKC')
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, '-')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 80) || 'project';
  return `${normalized}.project-transfer.zip`;
}

function readTableInfo(db: DatabaseSync): Map<string, TableInfo> {
  const tableNames = (db.prepare(`SELECT name FROM sqlite_schema
    WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name`).all() as Array<{ name: string }>)
    .map((row) => row.name)
    .filter(validIdentifier);
  const result = new Map<string, TableInfo>();
  for (const name of tableNames) {
    const columns = db.prepare(`PRAGMA table_info(${quoteIdentifier(name)})`).all() as unknown as ColumnInfo[];
    const foreignRows = db.prepare(`PRAGMA foreign_key_list(${quoteIdentifier(name)})`).all() as Array<{
      id: number;
      seq: number;
      table: string;
      from: string;
      to: string;
    }>;
    const grouped = new Map<number, ForeignKeyInfo>();
    for (const row of foreignRows) {
      if (!validIdentifier(row.table) || !validIdentifier(row.from) || !validIdentifier(row.to)) continue;
      const foreignKey = grouped.get(row.id) ?? { id: row.id, table: row.table, columns: [] };
      foreignKey.columns.push({ from: row.from, to: row.to });
      grouped.set(row.id, foreignKey);
    }
    for (const foreignKey of grouped.values()) {
      foreignKey.columns.sort((left, right) => foreignRows.find((row) => (
        row.id === foreignKey.id && row.from === left.from
      ))!.seq - foreignRows.find((row) => (
        row.id === foreignKey.id && row.from === right.from
      ))!.seq);
    }
    result.set(name, {
      name,
      columns,
      primaryKey: columns.filter((column) => column.pk > 0)
        .sort((left, right) => left.pk - right.pk)
        .map((column) => column.name),
      foreignKeys: [...grouped.values()],
    });
  }
  return result;
}

function collectProjectRows(
  db: DatabaseSync,
  schemas: Map<string, TableInfo>,
  project: SqlRow,
): Map<string, SqlRow[]> {
  const rows = new Map<string, SqlRow[]>([['projects', [project]]]);
  const rowKeys = new Map<string, Set<string>>([
    ['projects', new Set([rowIdentity(schemas.get('projects')!, project)])],
  ]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const schema of schemas.values()) {
      if (schema.name === 'projects' || EXCLUDED_TABLES.has(schema.name) || schema.name === 'settings') continue;
      for (const foreignKey of schema.foreignKeys) {
        const parentRows = rows.get(foreignKey.table);
        if (!parentRows?.length) continue;
        const tuples = uniqueTuples(parentRows, foreignKey.columns.map((column) => column.to));
        for (let index = 0; index < tuples.length; index += QUERY_TUPLE_BATCH_SIZE) {
          const batch = tuples.slice(index, index + QUERY_TUPLE_BATCH_SIZE);
          const where = batch.map(() => `(${foreignKey.columns
            .map((column) => `${quoteIdentifier(column.from)} = ?`).join(' AND ')})`).join(' OR ');
          const parameters = batch.flat();
          const found = db.prepare(`SELECT * FROM ${quoteIdentifier(schema.name)} WHERE ${where}`)
            .all(...parameters) as unknown as SqlRow[];
          if (appendUniqueRows(rows, rowKeys, schema, found)) changed = true;
        }
      }
    }
  }

  const settingsSchema = schemas.get('settings');
  if (settingsSchema) {
    const settings = db.prepare(`SELECT * FROM settings
      WHERE key LIKE ? ESCAPE '\\'`).all(`semantic\_binding\_ignored:${escapeLike(String(project.id))}:%`) as unknown as SqlRow[];
    appendUniqueRows(rows, rowKeys, settingsSchema, settings);
  }
  return rows;
}

function assertProjectExportIdle(
  db: DatabaseSync,
  schemas: Map<string, TableInfo>,
  projectId: string,
): void {
  if (!schemas.has('inflight_operations')) return;
  const active = db.prepare(`SELECT 1 FROM inflight_operations
    WHERE key LIKE ? ESCAPE '\\' AND expires_at > ? LIMIT 1`)
    .get(`${escapeLike(projectId)}:%`, new Date().toISOString());
  if (active) throw new ProjectTransferError('项目仍有后台任务在运行，请等待任务结束后再导出', 'project_busy');
}

function normalizeExportRows(
  rows: Map<string, SqlRow[]>,
  schemas: Map<string, TableInfo>,
  projectId: string,
): Map<string, SqlRow[]> {
  return new Map([...rows.entries()].map(([table, tableRows]) => {
    const schema = schemas.get(table)!;
    const normalized = tableRows.map((row) => Object.fromEntries(Object.entries(row).map(([column, value]) => {
      if (isAccountColumn(column, row) && value !== null && value !== '') return [column, SOURCE_OWNER];
      if (table === 'graph_import_uploads' && column === 'stored_path') {
        return [column, `project-transfer://graph-upload/${String(row.id)}`];
      }
      if (table === 'agent_candidate_workspaces'
        && ['credential_hash', 'credential_expires_at'].includes(column)) {
        return [column, null];
      }
      if (table === 'agent_candidate_workspaces' && column === 'lease_expires_at') {
        return [column, '1970-01-01T00:00:00.000Z'];
      }
      return [column, typeof value === 'string' ? normalizeProjectText(value, projectId) : cloneSqlValue(value)];
    })) as SqlRow)
      .sort((left, right) => rowIdentity(schema, left).localeCompare(rowIdentity(schema, right)));
    return [table, normalized];
  }));
}

async function collectGraphUploadFiles(rows: SqlRow[]): Promise<Array<{ uploadId: string; content: Uint8Array }>> {
  const result: Array<{ uploadId: string; content: Uint8Array }> = [];
  for (const row of rows) {
    const storedPath = String(row.stored_path ?? '');
    if (!storedPath || !existsSync(storedPath)) {
      throw new ProjectTransferError(`图谱导入文件不存在：${String(row.filename ?? row.id)}`);
    }
    const content = new Uint8Array(await readFile(storedPath));
    if (sha256(content) !== String(row.file_hash ?? '')) {
      throw new ProjectTransferError(`图谱导入文件校验失败：${String(row.filename ?? row.id)}`);
    }
    result.push({ uploadId: String(row.id), content });
  }
  return result;
}

async function readDirectoryFiles(root: string): Promise<Array<{ path: string; content: Uint8Array }>> {
  const result: Array<{ path: string; content: Uint8Array }> = [];
  const visit = async (directory: string) => {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name));
    for (const entry of entries) {
      const absolute = join(directory, entry.name);
      const metadata = await lstat(absolute);
      if (metadata.isSymbolicLink()) throw new ProjectTransferError('项目版本目录包含不支持的符号链接');
      if (metadata.isDirectory()) {
        await visit(absolute);
      } else if (metadata.isFile()) {
        const path = relative(root, absolute).split(sep).join('/');
        assertSafeArchivePath(path);
        if (path.endsWith('.lock')) {
          throw new ProjectTransferError('项目版本正在写入，请稍后再导出');
        }
        result.push({ path, content: new Uint8Array(await readFile(absolute)) });
      }
    }
  };
  await visit(root);
  return result;
}

function encodeRow(row: SqlRow): Record<string, unknown> {
  return Object.fromEntries(Object.entries(row).map(([column, value]) => [column, encodeValue(value)]));
}

function encodeValue(value: SqlValue): unknown {
  if (typeof value === 'bigint') return { $integer: value.toString() };
  if (value instanceof Uint8Array) return { $binary: Buffer.from(value).toString('base64') };
  return value;
}

function decodeValue(value: unknown): SqlValue {
  if (value === null || typeof value === 'string' || typeof value === 'number') return value;
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ProjectTransferError('迁移包数据库值格式不正确');
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length === 1 && typeof record.$binary === 'string') {
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(record.$binary)) {
      throw new ProjectTransferError('迁移包二进制数据格式不正确');
    }
    return new Uint8Array(Buffer.from(record.$binary, 'base64'));
  }
  if (Object.keys(record).length === 1 && typeof record.$integer === 'string' && /^-?\d+$/.test(record.$integer)) {
    return BigInt(record.$integer);
  }
  throw new ProjectTransferError('迁移包数据库值格式不正确');
}

function parseManifest(content: Uint8Array | undefined): TransferManifest {
  if (!content) throw new ProjectTransferError('迁移包缺少 manifest.json');
  let value: unknown;
  try {
    value = JSON.parse(strFromU8(content));
  } catch {
    throw new ProjectTransferError('迁移包清单格式不正确');
  }
  const manifest = value as Partial<TransferManifest>;
  if (manifest.format !== TRANSFER_FORMAT || manifest.version !== TRANSFER_VERSION) {
    throw new ProjectTransferError('迁移包版本不受支持');
  }
  if (!manifest.database || manifest.database.path !== DATABASE_PATH
    || !Array.isArray(manifest.database.tables) || !Number.isSafeInteger(manifest.database.rowCount)) {
    throw new ProjectTransferError('迁移包数据库清单不正确');
  }
  if (!manifest.project || typeof manifest.project.name !== 'string'
    || !Array.isArray(manifest.files) || !Array.isArray(manifest.graphUploads)
    || !Array.isArray(manifest.excluded)) {
    throw new ProjectTransferError('迁移包清单不完整');
  }
  if (manifest.agentRuntime !== undefined && (
    !manifest.agentRuntime || manifest.agentRuntime.path !== AGENT_RUNTIME_PATH
  )) {
    throw new ProjectTransferError('迁移包建模助手清单不正确');
  }
  for (const file of manifest.files) {
    if (!file || typeof file.path !== 'string' || !Number.isSafeInteger(file.size)
      || file.size < 0 || !/^[a-f0-9]{64}$/.test(String(file.sha256))) {
      throw new ProjectTransferError('迁移包文件清单不正确');
    }
    assertSafeArchivePath(file.path);
  }
  return manifest as TransferManifest;
}

function parseAgentRuntime(
  entries: Record<string, Uint8Array>,
  manifest: TransferManifest,
): Record<string, unknown> | undefined {
  if (!manifest.agentRuntime) return undefined;
  const content = entries[manifest.agentRuntime.path];
  if (!content) throw new ProjectTransferError('迁移包缺少建模助手数据');
  try {
    const value = JSON.parse(strFromU8(content));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid');
    return value as Record<string, unknown>;
  } catch {
    throw new ProjectTransferError('迁移包建模助手数据格式不正确');
  }
}

function parseTransferDatabase(content: Uint8Array | undefined): TransferDatabase {
  if (!content) throw new ProjectTransferError('迁移包缺少项目数据库');
  let value: unknown;
  try {
    value = JSON.parse(strFromU8(content));
  } catch {
    throw new ProjectTransferError('迁移包数据库格式不正确');
  }
  const database = value as Partial<TransferDatabase>;
  if (database.sourceProjectId !== SOURCE_PROJECT_ID || !database.tables
    || typeof database.tables !== 'object' || Array.isArray(database.tables)) {
    throw new ProjectTransferError('迁移包数据库结构不正确');
  }
  return database as TransferDatabase;
}

function validateArchiveEntries(entries: Record<string, Uint8Array>, manifest: TransferManifest): void {
  const expected = new Set([MANIFEST_PATH, ...manifest.files.map((file) => file.path)]);
  for (const path of Object.keys(entries)) {
    assertSafeArchivePath(path);
    if (!expected.has(path)) throw new ProjectTransferError(`迁移包包含未登记文件：${path}`);
  }
  for (const path of expected) {
    if (!entries[path]) throw new ProjectTransferError(`迁移包缺少文件：${path}`);
  }
  const graphPaths = new Set<string>();
  for (const graph of manifest.graphUploads) {
    if (!graph || typeof graph.uploadId !== 'string' || typeof graph.path !== 'string'
      || !graph.path.startsWith('graph-uploads/') || graphPaths.has(graph.path)) {
      throw new ProjectTransferError('迁移包图谱上传文件清单不正确');
    }
    graphPaths.add(graph.path);
    if (!expected.has(graph.path)) throw new ProjectTransferError('迁移包图谱上传文件未登记');
  }
}

function decodeAndValidateTables(
  database: TransferDatabase,
  manifest: TransferManifest,
  schemas: Map<string, TableInfo>,
): Map<string, SqlRow[]> {
  const tables = new Map<string, SqlRow[]>();
  let rowCount = 0;
  for (const [table, rawRows] of Object.entries(database.tables)) {
    if (!validIdentifier(table) || EXCLUDED_TABLES.has(table)) {
      throw new ProjectTransferError(`迁移包包含不允许导入的数据表：${table}`);
    }
    if (table === 'settings' && !rawRows.every((row) => (
      row && typeof row === 'object' && !Array.isArray(row)
      && typeof (row as Record<string, unknown>).key === 'string'
      && String((row as Record<string, unknown>).key).startsWith(`semantic_binding_ignored:${SOURCE_PROJECT_ID}:`)
    ))) {
      throw new ProjectTransferError('迁移包包含不允许导入的环境设置');
    }
    const schema = schemas.get(table);
    if (!schema) throw new ProjectTransferError(`目标环境版本过旧，缺少数据表：${table}`);
    if (!Array.isArray(rawRows)) throw new ProjectTransferError(`迁移包数据表格式不正确：${table}`);
    const allowedColumns = new Set(schema.columns.map((column) => column.name));
    const decoded = rawRows.map((rawRow) => {
      if (!rawRow || typeof rawRow !== 'object' || Array.isArray(rawRow)) {
        throw new ProjectTransferError(`迁移包数据行格式不正确：${table}`);
      }
      const row: SqlRow = {};
      for (const [column, value] of Object.entries(rawRow)) {
        if (!allowedColumns.has(column)) {
          throw new ProjectTransferError(`目标环境版本过旧，${table}.${column} 无法导入`);
        }
        row[column] = decodeValue(value);
      }
      for (const column of schema.columns) {
        if (!(column.name in row) && column.notnull && column.dflt_value === null) {
          throw new ProjectTransferError(`迁移包缺少必填字段：${table}.${column.name}`);
        }
      }
      return row;
    });
    rowCount += decoded.length;
    tables.set(table, decoded);
  }
  if (rowCount !== manifest.database.rowCount) throw new ProjectTransferError('迁移包数据库行数校验失败');
  if (!sameStringSet(new Set(tables.keys()), new Set(manifest.database.tables))) {
    throw new ProjectTransferError('迁移包数据库表清单不一致');
  }
  const projects = tables.get('projects');
  if (projects?.length !== 1 || projects[0].id !== SOURCE_PROJECT_ID || projects[0].owner !== SOURCE_OWNER) {
    throw new ProjectTransferError('迁移包项目根数据不正确');
  }
  for (const [table, tableRows] of tables) {
    if (table === 'projects' || table === 'settings') continue;
    for (const row of tableRows) {
      if ('project_id' in row && row.project_id !== SOURCE_PROJECT_ID) {
        throw new ProjectTransferError(`迁移包包含其他项目的数据：${table}`);
      }
    }
  }
  for (const row of tables.get('agent_candidate_workspaces') ?? []) {
    if (row.credential_hash || row.credential_expires_at) {
      throw new ProjectTransferError('迁移包包含不允许导入的运行凭据');
    }
  }
  return tables;
}

function validateProjectClosure(tables: Map<string, SqlRow[]>, schemas: Map<string, TableInfo>): void {
  const reachable = new Map<string, Set<string>>();
  const projectSchema = schemas.get('projects')!;
  reachable.set('projects', new Set(tables.get('projects')!.map((row) => rowIdentity(projectSchema, row))));
  if (tables.has('settings')) {
    reachable.set('settings', new Set(tables.get('settings')!.map((row) => rowIdentity(schemas.get('settings')!, row))));
  }
  let changed = true;
  while (changed) {
    changed = false;
    for (const [table, tableRows] of tables) {
      if (reachable.get(table)?.size === tableRows.length) continue;
      const schema = schemas.get(table)!;
      for (const row of tableRows) {
        const identity = rowIdentity(schema, row);
        if (reachable.get(table)?.has(identity)) continue;
        const attached = schema.foreignKeys.some((foreignKey) => {
          const parentRows = tables.get(foreignKey.table);
          const parentReachable = reachable.get(foreignKey.table);
          if (!parentRows || !parentReachable) return false;
          return parentRows.some((parent) => parentReachable.has(rowIdentity(schemas.get(foreignKey.table)!, parent))
            && foreignKey.columns.every((column) => sqlValuesEqual(row[column.from], parent[column.to])));
        });
        if (attached) {
          const tableReachable = reachable.get(table) ?? new Set<string>();
          tableReachable.add(identity);
          reachable.set(table, tableReachable);
          changed = true;
        }
      }
    }
  }
  for (const [table, tableRows] of tables) {
    if ((reachable.get(table)?.size ?? 0) !== tableRows.length) {
      throw new ProjectTransferError(`迁移包包含无法归属于项目的数据：${table}`);
    }
  }

  for (const [table, tableRows] of tables) {
    const schema = schemas.get(table)!;
    for (const row of tableRows) {
      for (const foreignKey of schema.foreignKeys) {
        const values = foreignKey.columns.map((column) => row[column.from]);
        if (values.every((value) => value === null || value === undefined)) continue;
        const parents = tables.get(foreignKey.table);
        if (!parents?.some((parent) => foreignKey.columns.every(
          (column) => sqlValuesEqual(row[column.from], parent[column.to]),
        ))) {
          throw new ProjectTransferError(`迁移包关联数据不完整：${table}`);
        }
      }
    }
  }
}

function buildIdMap(
  tables: Map<string, SqlRow[]>,
  schemas: Map<string, TableInfo>,
  projectId: string,
): Map<string, string> {
  const idMap = new Map<string, string>([[SOURCE_PROJECT_ID, projectId]]);
  for (const [table, tableRows] of tables) {
    if (table === 'settings') continue;
    const schema = schemas.get(table)!;
    if (schema.primaryKey.length !== 1) continue;
    const primaryKey = schema.primaryKey[0];
    const column = schema.columns.find((item) => item.name === primaryKey)!;
    if (!column.type.toUpperCase().includes('TEXT')) continue;
    for (const row of tableRows) {
      const value = row[primaryKey];
      if (typeof value === 'string' && value && !idMap.has(value)) {
        idMap.set(value, createPortableId(value));
      }
    }
  }
  return idMap;
}

function transformImportRows(
  tables: Map<string, SqlRow[]>,
  schemas: Map<string, TableInfo>,
  idMap: Map<string, string>,
  owner: string,
  projectId: string,
): Map<string, SqlRow[]> {
  return new Map([...tables.entries()].map(([table, tableRows]) => {
    const schema = schemas.get(table)!;
    const transformed = tableRows.map((row) => {
      const result: SqlRow = {};
      for (const [column, value] of Object.entries(row)) {
        if (isAccountColumn(column, row) && value !== null && value !== '') {
          result[column] = owner;
          continue;
        }
        result[column] = transformSqlValue(value, idMap, projectId);
      }
      return result;
    }).sort((left, right) => rowIdentity(schema, left).localeCompare(rowIdentity(schema, right)));
    return [table, transformed];
  }));
}

function applyImportedIdMap(
  tables: Map<string, SqlRow[]>,
  values: Record<string, string>,
  projectId: string,
): void {
  const idMap = new Map(Object.entries(values).filter(([source, target]) => source && target));
  for (const tableRows of tables.values()) {
    for (const row of tableRows) {
      for (const [column, value] of Object.entries(row)) {
        row[column] = transformSqlValue(value, idMap, projectId);
      }
    }
  }
}

function transformSqlValue(value: SqlValue, idMap: Map<string, string>, projectId: string): SqlValue {
  if (typeof value !== 'string') return cloneSqlValue(value);
  const mapped = idMap.get(value);
  if (mapped) return mapped;
  const withProject = value.includes(SOURCE_PROJECT_ID)
    ? value.replaceAll(SOURCE_PROJECT_ID, projectId)
    : value;
  if (!looksLikeJson(withProject)) return withProject;
  try {
    const parsed = JSON.parse(withProject);
    return JSON.stringify(transformJsonValue(parsed, idMap, projectId));
  } catch {
    return withProject;
  }
}

function transformJsonValue(value: unknown, idMap: Map<string, string>, projectId: string): unknown {
  if (typeof value === 'string') {
    return idMap.get(value) ?? value.replaceAll(SOURCE_PROJECT_ID, projectId);
  }
  if (Array.isArray(value)) return value.map((item) => transformJsonValue(item, idMap, projectId));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => [key, transformJsonValue(item, idMap, projectId)]));
  }
  return value;
}

function prepareGraphUploadTargets(input: {
  manifest: TransferManifest;
  entries: Record<string, Uint8Array>;
  tables: Map<string, SqlRow[]>;
  dataDir: string;
  idMap: Map<string, string>;
}): Array<{ stagedPath?: string; finalPath: string; content: Uint8Array }> {
  const graphRows = input.tables.get('graph_import_uploads') ?? [];
  if (graphRows.length !== input.manifest.graphUploads.length) {
    throw new ProjectTransferError('迁移包图谱上传文件数量不一致');
  }
  const byOriginalId = new Map(input.manifest.graphUploads.map((item) => [item.uploadId, item]));
  return graphRows.map((row) => {
    const originalId = [...input.idMap.entries()].find(([, mapped]) => mapped === row.id)?.[0];
    const descriptor = originalId ? byOriginalId.get(originalId) : undefined;
    if (!descriptor) throw new ProjectTransferError('迁移包图谱上传文件关联不完整');
    const content = input.entries[descriptor.path];
    if (!content || sha256(content) !== String(row.file_hash ?? '')) {
      throw new ProjectTransferError('迁移包图谱上传文件校验失败');
    }
    const extension = ['.xls', '.xlsx'].includes(extname(String(row.filename ?? '')).toLowerCase())
      ? extname(String(row.filename)).toLowerCase()
      : '.xlsx';
    const finalPath = join(input.dataDir, 'tmp', 'graph-import', `${String(row.id)}${extension}`);
    row.stored_path = finalPath;
    const expiresAt = Date.parse(String(row.expires_at ?? ''));
    if (!Number.isFinite(expiresAt) || expiresAt < Date.now() + GRAPH_UPLOAD_TTL_MS) {
      row.expires_at = new Date(Date.now() + GRAPH_UPLOAD_TTL_MS).toISOString();
    }
    return { finalPath, content };
  });
}

async function validateArtifactBundle(
  tables: Map<string, SqlRow[]>,
  artifactEntryCount: number,
  stagedArtifactDir: string,
): Promise<void> {
  const revisions = tables.get('artifact_revisions') ?? [];
  if (revisions.length && artifactEntryCount === 0) {
    throw new ProjectTransferError('迁移包缺少项目版本历史');
  }
  if (artifactEntryCount && !existsSync(join(stagedArtifactDir, '.git', 'HEAD'))) {
    throw new ProjectTransferError('迁移包项目版本历史不完整');
  }
  if (!artifactEntryCount) return;
  const projectId = basename(stagedArtifactDir);
  const repository = new IsomorphicGitArtifactRepository({ rootDir: dirname(stagedArtifactDir) });
  let currentOid = '';
  try {
    currentOid = (await repository.current(projectId)).oid;
    for (const revision of revisions) {
      await repository.snapshot(projectId, String(revision.oid ?? ''));
    }
  } catch {
    throw new ProjectTransferError('迁移包项目版本历史已经损坏');
  }
  if (revisions.length && !revisions.some((revision) => revision.oid === currentOid)) {
    throw new ProjectTransferError('迁移包项目版本索引与 Git 历史不一致');
  }
}

function insertTransferRows(
  db: DatabaseSync,
  tables: Map<string, SqlRow[]>,
  schemas: Map<string, TableInfo>,
): void {
  const orderedTables = ['projects', ...[...tables.keys()].filter((table) => table !== 'projects').sort()];
  for (const table of orderedTables) {
    const tableRows = tables.get(table);
    if (!tableRows?.length) continue;
    const schema = schemas.get(table)!;
    for (const row of tableRows) {
      const columns = schema.columns.map((column) => column.name).filter((column) => column in row);
      const placeholders = columns.map(() => '?').join(', ');
      db.prepare(`INSERT INTO ${quoteIdentifier(table)} (${columns.map(quoteIdentifier).join(', ')})
        VALUES (${placeholders})`).run(...columns.map((column) => row[column]));
    }
  }
}

function inspectZipDirectory(archive: Uint8Array): void {
  const buffer = Buffer.from(archive.buffer, archive.byteOffset, archive.byteLength);
  const minimumOffset = Math.max(0, buffer.length - 65_557);
  let endOffset = -1;
  for (let offset = buffer.length - 22; offset >= minimumOffset; offset -= 1) {
    if (buffer.readUInt32LE(offset) === 0x06054b50) {
      endOffset = offset;
      break;
    }
  }
  if (endOffset < 0) throw new ProjectTransferError('项目迁移包不是有效的 ZIP 文件');
  const entryCount = buffer.readUInt16LE(endOffset + 10);
  const directorySize = buffer.readUInt32LE(endOffset + 12);
  const directoryOffset = buffer.readUInt32LE(endOffset + 16);
  if (entryCount > MAX_ARCHIVE_ENTRIES || directoryOffset + directorySize > endOffset) {
    throw new ProjectTransferError('项目迁移包目录结构不正确');
  }
  let offset = directoryOffset;
  let uncompressedTotal = 0;
  const names = new Set<string>();
  for (let index = 0; index < entryCount; index += 1) {
    if (offset + 46 > buffer.length || buffer.readUInt32LE(offset) !== 0x02014b50) {
      throw new ProjectTransferError('项目迁移包目录结构不正确');
    }
    const flags = buffer.readUInt16LE(offset + 8);
    const uncompressedSize = buffer.readUInt32LE(offset + 24);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    if ((flags & 1) !== 0 || uncompressedSize === 0xffffffff) {
      throw new ProjectTransferError('项目迁移包使用了不支持的 ZIP 格式');
    }
    const nameStart = offset + 46;
    const nameEnd = nameStart + nameLength;
    if (nameEnd > buffer.length) throw new ProjectTransferError('项目迁移包目录结构不正确');
    const name = buffer.subarray(nameStart, nameEnd).toString('utf8');
    assertSafeArchivePath(name);
    if (names.has(name)) throw new ProjectTransferError(`迁移包包含重复文件：${name}`);
    names.add(name);
    uncompressedTotal += uncompressedSize;
    if (uncompressedTotal > MAX_UNCOMPRESSED_BYTES) throw new ProjectTransferError('项目迁移包解压后超过大小限制');
    offset = nameEnd + extraLength + commentLength;
  }
  if (offset !== directoryOffset + directorySize) throw new ProjectTransferError('项目迁移包目录结构不正确');
}

function safeArchiveTarget(root: string, archivePath: string): string {
  assertSafeArchivePath(archivePath);
  const resolvedRoot = resolve(root);
  const target = resolve(resolvedRoot, ...archivePath.split('/'));
  if (target !== resolvedRoot && !target.startsWith(`${resolvedRoot}${sep}`)) {
    throw new ProjectTransferError('迁移包文件路径不安全');
  }
  return target;
}

function assertSafeArchivePath(path: string): void {
  if (!path || path.includes('\\') || path.includes('\0') || path.startsWith('/')
    || /^[A-Za-z]:/.test(path) || path.split('/').some((part) => !part || part === '.' || part === '..')) {
    throw new ProjectTransferError('迁移包文件路径不安全');
  }
}

function appendUniqueRows(
  rows: Map<string, SqlRow[]>,
  rowKeys: Map<string, Set<string>>,
  schema: TableInfo,
  incoming: SqlRow[],
): boolean {
  const tableRows = rows.get(schema.name) ?? [];
  const keys = rowKeys.get(schema.name) ?? new Set<string>();
  let changed = false;
  for (const row of incoming) {
    const key = rowIdentity(schema, row);
    if (keys.has(key)) continue;
    keys.add(key);
    tableRows.push(row);
    changed = true;
  }
  if (changed) {
    rows.set(schema.name, tableRows);
    rowKeys.set(schema.name, keys);
  }
  return changed;
}

function uniqueTuples(rows: SqlRow[], columns: string[]): SqlValue[][] {
  const result: SqlValue[][] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    const tuple = columns.map((column) => row[column]);
    if (tuple.some((value) => value === null || value === undefined)) continue;
    const key = tuple.map(stableSqlValue).join('|');
    if (!seen.has(key)) {
      seen.add(key);
      result.push(tuple);
    }
  }
  return result;
}

function rowIdentity(schema: TableInfo, row: SqlRow): string {
  const columns = schema.primaryKey.length ? schema.primaryKey : schema.columns.map((column) => column.name);
  return columns.map((column) => `${column}:${stableSqlValue(row[column])}`).join('|');
}

function stableSqlValue(value: SqlValue | undefined): string {
  if (value instanceof Uint8Array) return `b:${Buffer.from(value).toString('base64')}`;
  if (typeof value === 'bigint') return `i:${value.toString()}`;
  return `${typeof value}:${String(value)}`;
}

function sqlValuesEqual(left: SqlValue | undefined, right: SqlValue | undefined): boolean {
  return stableSqlValue(left) === stableSqlValue(right);
}

function normalizeProjectText(value: string, projectId: string): string {
  return value.includes(projectId) ? value.replaceAll(projectId, SOURCE_PROJECT_ID) : value;
}

function cloneSqlValue(value: SqlValue): SqlValue {
  return value instanceof Uint8Array ? new Uint8Array(value) : value;
}

function sha256(content: Uint8Array): string {
  return createHash('sha256').update(content).digest('hex');
}

function quoteIdentifier(identifier: string): string {
  if (!validIdentifier(identifier)) throw new ProjectTransferError(`数据库标识符不正确：${identifier}`);
  return `"${identifier}"`;
}

function validIdentifier(value: string): boolean {
  return /^[A-Za-z_][A-Za-z0-9_]*$/.test(value);
}

function createProjectId(): string {
  return `pj_${randomUUID().slice(0, 12)}`;
}

function createPortableId(source: string): string {
  const prefix = source.match(/^([A-Za-z][A-Za-z0-9]{0,11})_/i)?.[1]?.toLowerCase() || 'id';
  return `${prefix}_${randomUUID().replaceAll('-', '').slice(0, 20)}`;
}

function looksLikeJson(value: string): boolean {
  const trimmed = value.trim();
  return (trimmed.startsWith('{') && trimmed.endsWith('}'))
    || (trimmed.startsWith('[') && trimmed.endsWith(']'));
}

function isAccountColumn(column: string, row: SqlRow): boolean {
  if (ACCOUNT_COLUMNS.has(column)) return true;
  if (column === 'actor_name') return row.actor_kind === 'user';
  if (column === 'decision_actor_name') return row.decision_actor_kind === 'user';
  return false;
}

function sameStringSet(left: Set<string>, right: Set<string>): boolean {
  return left.size === right.size && [...left].every((value) => right.has(value));
}

function escapeLike(value: string): string {
  return value.replace(/[\\%_]/g, (character) => `\\${character}`);
}
