import { parseExcel } from '../documents/parser.js';
import { getProjectDataSource } from '../evaluation/data-source-repo.js';
import { latestPackageVersion } from '../workflow/m2-repo.js';
import { getOverrides } from '../workflow/package-chat.js';
import { renderPackage } from '../workflow/package-render.js';
import { extractGraphImportSchema } from './package-schema.js';
import {
  createGraphImportRun,
  finishGraphImportRun,
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
  GraphImportValidation,
} from './types.js';
import { normalizeGraphImportMapping, validateGraphImportMapping } from './validator.js';

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const MAX_ROWS_PER_SHEET = 50_000;

export interface GraphImportSheetPreview {
  name: string;
  columns: string[];
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
  for (const [path, content] of Object.entries(getOverrides(projectId))) {
    if (path in files) files[path] = content;
  }
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
  });
  return { upload, tables };
}

function sheetPreviews(tables: ReturnType<typeof parseExcel>): GraphImportSheetPreview[] {
  return tables.map((table) => ({
    name: table.name,
    columns: table.fields.map((field) => field.name),
    rowCount: table.rowCount,
    sampleRows: (table.rows ?? []).slice(0, 3).map((row) => Object.fromEntries(
      table.fields.map((field, index) => [field.name, String(row[index] ?? '')]),
    )),
  }));
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
}): Promise<void> {
  const { request, run, plan, connection, writer } = input;
  try {
    const result = await writer(plan, connection, {
      replaceExisting: request.replaceExisting === true,
      userName: request.owner,
    });
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
      status: 'failed',
      skippedCount: counts(plan).skippedCount,
      error: safeError(error, request.password),
    });
  }
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
  schedule(() => runGraphImportTask({
    request: { ...input, mapping, password },
    run,
    plan,
    connection,
    writer,
  }));
  return { run };
}

export function cancelGraphImportUpload(input: {
  projectId: string;
  owner: string;
  uploadId: string;
}): void {
  deleteGraphImportUpload(input);
}

export function getGraphImportHistory(projectId: string): GraphImportRun[] {
  return listGraphImportRuns(projectId);
}
