import { parseExcel } from '../documents/parser.js';
import { getProjectDataSource } from '../evaluation/data-source-repo.js';
import { latestPackageVersion } from '../workflow/m2-repo.js';
import { renderPackage } from '../workflow/package-render.js';
import { extractGraphImportSchema } from './package-schema.js';
import {
  createGraphImportRun,
  finishGraphImportRun,
  getGraphImportRun,
  getRunningGraphImportRun,
  listGraphImportRuns,
  type GraphImportRun,
} from './repo.js';
import {
  GraphIsolationDataExistsError,
  inspectGraphIsolation,
  writeGraphImport,
  type GraphIsolationCounts,
  type Neo4jGraphImportConnection,
  type Neo4jGraphImportWriteOptions,
} from './neo4j-writer.js';
import {
  deleteGraphImportUpload,
  readGraphImportUpload,
  saveGraphImportUpload,
} from './upload-store.js';
import type {
  GraphImportMapping,
  GraphImportPackageSchema,
  GraphImportPropertyType,
  GraphImportValidation,
} from './types.js';
import { normalizeGraphImportMapping, validateGraphImportMapping } from './validator.js';
import { inferGraphImportColumns } from './value-inference.js';

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const MAX_ROWS_PER_SHEET = 50_000;

export interface GraphImportSheetPreview {
  name: string;
  columns: string[];
  columnTypes: Record<string, GraphImportPropertyType>;
  rowCount: number;
  sampleRows: Record<string, string>[];
}

export interface GraphImportUploadPreview {
  id: string;
  filename: string;
  fileHash: string;
  sizeBytes: number;
  expiresAt: string;
  packageVersion: string;
  sheets: GraphImportSheetPreview[];
  schema: GraphImportPackageSchema;
}

export interface GraphImportCounts {
  nodeCount: number;
  relationshipCount: number;
  skippedCount: number;
  skippedBlankCount: number;
  skippedUnmatchedCount: number;
}

export type GraphImportWriter = (
  plan: GraphImportValidation,
  connection: Neo4jGraphImportConnection,
  options: Neo4jGraphImportWriteOptions,
) => Promise<{ nodeCount: number; relationshipCount: number }>;

export type GraphImportIsolationInspector = (
  connection: Neo4jGraphImportConnection,
) => Promise<GraphIsolationCounts>;

export type GraphImportTaskScheduler = (task: () => Promise<void>) => void;

export interface GraphImportExecutionDependencies {
  writer?: GraphImportWriter;
  inspectIsolation?: GraphImportIsolationInspector;
  schedule?: GraphImportTaskScheduler;
}

interface ActiveGraphImportTask {
  projectId: string;
  uploadId: string;
  runId: string;
  controller: AbortController;
  completion: Promise<void>;
  resolveCompletion: () => void;
  started: boolean;
  settled: boolean;
}

const activeGraphImports = new Map<string, ActiveGraphImportTask>();

export class GraphImportAlreadyRunningError extends Error {
  readonly code = 'GRAPH_IMPORT_ALREADY_RUNNING';
  constructor(readonly run: GraphImportRun) {
    super('当前项目已有图数据导入任务正在后台执行');
    this.name = 'GraphImportAlreadyRunningError';
  }
}

const scheduleBackgroundTask: GraphImportTaskScheduler = (task) => {
  setImmediate(() => { void task(); });
};

function text(value: unknown): string {
  return String(value ?? '').trim();
}

function loadPackage(projectId: string): {
  version: string;
  schema: GraphImportPackageSchema;
} {
  const latest = latestPackageVersion(projectId);
  if (!latest) throw new Error('请先生成知识包，再导入图数据');
  const files = renderPackage(latest.spec);
  const schema = extractGraphImportSchema(latest.spec, files);
  return { version: latest.version, schema };
}

function loadConnection(
  projectId: string,
  password: string,
  requirePassword = true,
): Neo4jGraphImportConnection {
  const dataSource = getProjectDataSource(projectId);
  if (dataSource?.mode !== 'connection' || dataSource.provider !== 'neo4j') {
    throw new Error('请先保存 Neo4j 数据源配置');
  }
  if (requirePassword && !password) throw new Error('本次导入的 Neo4j Password 不能为空');
  const config = dataSource.config;
  const connection: Neo4jGraphImportConnection = {
    uri: text(config.uri),
    username: text(config.username),
    password,
    database: text(config.database) || 'neo4j',
    workspaceProperty: text(config.workspace_property),
    workspaceValue: text(config.workspace_value),
    ontologyProperty: text(config.ontology_property),
    ontologyValue: text(config.ontology_value),
  };
  const missing = [
    ['Neo4j URL', connection.uri],
    ['Neo4j User', connection.username],
    ['Workspace 隔离字段', connection.workspaceProperty],
    ['Workspace 隔离值', connection.workspaceValue],
    ['Ontology 隔离字段', connection.ontologyProperty],
    ['Ontology 隔离值', connection.ontologyValue],
  ].find(([, value]) => !value);
  if (missing) throw new Error(`${missing[0]}未配置，请先重新保存 Neo4j 数据源`);
  return connection;
}

function parseUpload(input: { projectId: string; owner: string; uploadId: string }) {
  const { upload, buffer } = readGraphImportUpload(input);
  const tables = parseExcel(buffer, {
    maxRows: MAX_ROWS_PER_SHEET,
    rejectOverflow: true,
    includeCellMetadata: true,
  });
  return { upload, tables };
}

function sheetPreviews(tables: ReturnType<typeof parseExcel>): GraphImportSheetPreview[] {
  return tables.map((table) => {
    const profiles = inferGraphImportColumns(table);
    return {
      name: table.name,
      columns: table.fields.map((field) => field.name),
      columnTypes: Object.fromEntries(table.fields.map((field, index) => [
        field.name,
        profiles[index].type,
      ])),
      rowCount: table.rowCount,
      sampleRows: (table.rows ?? []).slice(0, 3).map((row) => Object.fromEntries(
        table.fields.map((field, index) => [field.name, String(row[index] ?? '')]),
      )),
    };
  });
}

function counts(plan: GraphImportValidation): GraphImportCounts {
  const skippedCount = plan.skippedBlankJoinCount + plan.skippedUnmatchedJoinCount;
  return {
    nodeCount: plan.nodeCount,
    relationshipCount: plan.relationshipCount,
    skippedCount,
    skippedBlankCount: plan.skippedBlankJoinCount,
    skippedUnmatchedCount: plan.skippedUnmatchedJoinCount,
  };
}

function safeError(error: unknown, password: string): string {
  let message = error instanceof Error ? error.message : String(error);
  if (password) message = message.split(password).join('[REDACTED]');
  return message.slice(0, 2_000);
}

export function uploadGraphImportExcel(input: {
  projectId: string;
  owner: string;
  filename: string;
  buffer: Buffer;
}): GraphImportUploadPreview {
  if (input.buffer.byteLength > MAX_FILE_SIZE) throw new Error('Excel 文件不能超过 50MB');
  const packageContext = loadPackage(input.projectId);
  loadConnection(input.projectId, '', false);
  const tables = parseExcel(input.buffer, {
    maxRows: MAX_ROWS_PER_SHEET,
    rejectOverflow: true,
    includeCellMetadata: true,
  });
  if (!tables.length) throw new Error('Excel 中没有可导入的 Sheet');
  const upload = saveGraphImportUpload(input);
  return {
    id: upload.id,
    filename: upload.filename,
    fileHash: upload.fileHash,
    sizeBytes: upload.sizeBytes,
    expiresAt: upload.expiresAt,
    packageVersion: packageContext.version,
    sheets: sheetPreviews(tables),
    schema: packageContext.schema,
  };
}

export function validateGraphImportUpload(input: {
  projectId: string;
  owner: string;
  uploadId: string;
  mapping: GraphImportMapping;
}): { packageVersion: string; counts: GraphImportCounts; warnings: string[] } {
  const packageContext = loadPackage(input.projectId);
  const { tables } = parseUpload(input);
  const plan = validateGraphImportMapping(packageContext.schema, tables, input.mapping);
  return { packageVersion: packageContext.version, counts: counts(plan), warnings: plan.warnings };
}

async function runGraphImportTask(input: {
  request: {
    projectId: string;
    owner: string;
    uploadId: string;
    mapping: GraphImportMapping;
    password: string;
    replaceExisting?: boolean;
  };
  run: GraphImportRun;
  plan: GraphImportValidation;
  connection: Neo4jGraphImportConnection;
  writer: GraphImportWriter;
  signal: AbortSignal;
}): Promise<void> {
  const { request, run, plan, connection, writer, signal } = input;
  try {
    if (signal.aborted) throw signal.reason;
    const result = await writer(plan, connection, {
      replaceExisting: request.replaceExisting === true,
      userName: request.owner,
      signal,
    });
    if (signal.aborted) throw signal.reason;
    finishGraphImportRun(run.id, {
      status: 'done',
      nodeCount: result.nodeCount,
      relationshipCount: result.relationshipCount,
      skippedCount: counts(plan).skippedCount,
    });
    try {
      deleteGraphImportUpload(request);
    } catch {
      // The import is already committed; an expired temporary file is cleaned up later.
    }
  } catch (error) {
    finishGraphImportRun(run.id, {
      status: signal.aborted ? 'cancelled' : 'failed',
      skippedCount: counts(plan).skippedCount,
      error: signal.aborted ? '图数据导入已取消' : safeError(error, request.password),
    });
  }
}

function settleGraphImportTask(task: ActiveGraphImportTask): void {
  if (task.settled) return;
  task.settled = true;
  if (activeGraphImports.get(task.projectId) === task) activeGraphImports.delete(task.projectId);
  task.resolveCompletion();
}

async function cancelActiveGraphImport(task: ActiveGraphImportTask, reason: string): Promise<GraphImportRun> {
  if (!task.settled) {
    task.controller.abort(new Error(reason));
    if (!task.started) {
      const current = getGraphImportRun(task.runId);
      if (current?.status === 'running') {
        finishGraphImportRun(task.runId, { status: 'cancelled', error: reason });
      }
      settleGraphImportTask(task);
    }
  }
  await task.completion;
  const run = getGraphImportRun(task.runId);
  if (!run) throw new Error('图数据导入任务不存在');
  return run;
}

export async function executeGraphImport(input: {
  projectId: string;
  owner: string;
  uploadId: string;
  mapping: GraphImportMapping;
  password: string;
  replaceExisting?: boolean;
}, dependencies: GraphImportExecutionDependencies = {}): Promise<{ run: GraphImportRun }> {
  const running = getRunningGraphImportRun(input.projectId);
  if (running) throw new GraphImportAlreadyRunningError(running);

  const password = String(input.password ?? '');
  const connection = loadConnection(input.projectId, password);
  const packageContext = loadPackage(input.projectId);
  const { upload, tables } = parseUpload(input);
  const mapping = normalizeGraphImportMapping(input.mapping);
  const plan = validateGraphImportMapping(packageContext.schema, tables, mapping);
  const inspectIsolation = dependencies.inspectIsolation ?? inspectGraphIsolation;
  if (input.replaceExisting !== true) {
    let existing: GraphIsolationCounts;
    try {
      existing = await inspectIsolation(connection);
    } catch (error) {
      throw new Error(safeError(error, password));
    }
    if (existing.nodeCount || existing.relationshipCount) {
      throw new GraphIsolationDataExistsError(existing);
    }
  }
  const run = createGraphImportRun({
    projectId: input.projectId,
    filename: upload.filename,
    fileHash: upload.fileHash,
    packageVersion: packageContext.version,
    mapping,
  });
  const writer = dependencies.writer ?? writeGraphImport;
  const schedule = dependencies.schedule ?? scheduleBackgroundTask;
  let resolveCompletion: () => void = () => {};
  const completion = new Promise<void>((resolve) => { resolveCompletion = resolve; });
  const task: ActiveGraphImportTask = {
    projectId: input.projectId,
    uploadId: input.uploadId,
    runId: run.id,
    controller: new AbortController(),
    completion,
    resolveCompletion,
    started: false,
    settled: false,
  };
  activeGraphImports.set(input.projectId, task);
  try {
    schedule(async () => {
      if (task.settled) return;
      task.started = true;
      try {
        await runGraphImportTask({
          request: { ...input, mapping, password },
          run,
          plan,
          connection,
          writer,
          signal: task.controller.signal,
        });
      } finally {
        settleGraphImportTask(task);
      }
    });
  } catch (error) {
    finishGraphImportRun(run.id, {
      status: 'failed',
      skippedCount: counts(plan).skippedCount,
      error: safeError(error, password),
    });
    settleGraphImportTask(task);
    throw error;
  }
  return { run };
}

export async function cancelGraphImportUpload(input: {
  projectId: string;
  owner: string;
  uploadId: string;
}): Promise<void> {
  const active = activeGraphImports.get(input.projectId);
  if (active?.uploadId === input.uploadId) {
    await cancelActiveGraphImport(active, '用户已取消本次图数据导入');
  }
  deleteGraphImportUpload(input);
}

export async function cancelGraphImportRun(projectId: string, runId: string): Promise<GraphImportRun> {
  const active = activeGraphImports.get(projectId);
  if (!active || active.runId !== runId) throw new Error('该项目当前没有可取消的图数据导入任务');
  return cancelActiveGraphImport(active, '用户已取消本次图数据导入');
}

export async function cancelGraphImportsForProject(projectId: string): Promise<void> {
  const active = activeGraphImports.get(projectId);
  if (!active) return;
  await cancelActiveGraphImport(active, '项目删除，图数据导入已取消');
}

export function getGraphImportHistory(projectId: string): GraphImportRun[] {
  return listGraphImportRuns(projectId);
}
