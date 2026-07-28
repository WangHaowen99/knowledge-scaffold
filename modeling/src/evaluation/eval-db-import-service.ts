import { getProjectDataSource } from './data-source-repo.js';
import {
  normalizeEvaluationDatabaseConfig,
  type EvaluationDataSourceProvider,
} from './data-source.js';
import {
  importEvaluationDatabaseExcel,
  validateEvaluationDatabaseExcelUpload,
  type EvaluationDatabaseDependencies,
} from './eval-db.js';
import { importEvaluationDatabaseExcelInWorker } from './eval-db-import-worker-client.js';
import {
  createEvaluationDatabaseImportRun,
  finishEvaluationDatabaseImportRun,
  getRunningEvaluationDatabaseImportRun,
  getRunningEvaluationDatabaseImportCount,
  listEvaluationDatabaseImportRuns,
  type EvaluationDatabaseImportRun,
} from './eval-db-import-repo.js';

const NL2SQL_PROVIDERS = new Set<EvaluationDataSourceProvider>([
  'nl2sql_template',
  'nl2sql_http',
]);
const MAX_CONCURRENT_EVALUATION_DATABASE_IMPORTS = 1;

export type EvaluationDatabaseImportTaskScheduler = (task: () => Promise<void>) => void;

export interface EvaluationDatabaseImportServiceDependencies
  extends EvaluationDatabaseDependencies {
  schedule?: EvaluationDatabaseImportTaskScheduler;
  importExcel?: typeof importEvaluationDatabaseExcel;
}

export class EvaluationDatabaseImportAlreadyRunningError extends Error {
  readonly code = 'EVALUATION_DATABASE_IMPORT_ALREADY_RUNNING';

  constructor(readonly run: EvaluationDatabaseImportRun) {
    super('当前项目已有评测库导入任务正在后台执行');
    this.name = 'EvaluationDatabaseImportAlreadyRunningError';
  }
}

export class EvaluationDatabaseImportCapacityError extends Error {
  readonly code = 'EVALUATION_DATABASE_IMPORT_CAPACITY_EXCEEDED';

  constructor() {
    super('当前已有评测库导入任务正在执行，请等待完成后重试');
    this.name = 'EvaluationDatabaseImportCapacityError';
  }
}

const scheduleBackgroundTask: EvaluationDatabaseImportTaskScheduler = (task) => {
  setImmediate(() => { void task().catch(() => undefined); });
};

function safeError(error: unknown, password: string): string {
  let message = error instanceof Error ? error.message : String(error);
  if (password) message = message.split(password).join('[REDACTED]');
  return message.slice(0, 2_000);
}

function loadEvaluationDatabaseConfig(projectId: string, password: string) {
  const dataSource = getProjectDataSource(projectId);
  if (dataSource?.mode !== 'connection'
    || !dataSource.provider
    || !NL2SQL_PROVIDERS.has(dataSource.provider)) {
    throw new Error('请先保存并测试 NL2SQL 数据源配置');
  }
  const savedDatabase = dataSource.config.evaluation_database;
  return normalizeEvaluationDatabaseConfig({
    ...(savedDatabase && typeof savedDatabase === 'object' ? savedDatabase : {}),
    password,
  });
}

async function runEvaluationDatabaseImportTask(input: {
  run: EvaluationDatabaseImportRun;
  filename: string;
  buffer: Buffer;
  password: string;
  config: ReturnType<typeof loadEvaluationDatabaseConfig>;
  dependencies: EvaluationDatabaseImportServiceDependencies;
}): Promise<void> {
  try {
    const importInput = {
      filename: input.filename,
      buffer: input.buffer,
      config: input.config,
    };
    let result;
    if (input.dependencies.importExcel) {
      result = await input.dependencies.importExcel(importInput, {
        connect: input.dependencies.connect,
      });
    } else if (input.dependencies.connect) {
      result = await importEvaluationDatabaseExcel(importInput, {
        connect: input.dependencies.connect,
      });
    } else {
      const workerResult = importEvaluationDatabaseExcelInWorker(importInput);
      input.buffer = Buffer.alloc(0);
      result = await workerResult;
    }
    finishEvaluationDatabaseImportRun(input.run.id, { result });
  } catch (error) {
    finishEvaluationDatabaseImportRun(input.run.id, {
      error: safeError(error, input.password),
    });
  }
}

export function assertProjectEvaluationDatabaseImportAvailable(projectId: string): void {
  const running = getRunningEvaluationDatabaseImportRun(projectId);
  if (running) throw new EvaluationDatabaseImportAlreadyRunningError(running);
  if (getRunningEvaluationDatabaseImportCount() >= MAX_CONCURRENT_EVALUATION_DATABASE_IMPORTS) {
    throw new EvaluationDatabaseImportCapacityError();
  }
}

export function startProjectEvaluationDatabaseImport(
  input: {
    projectId: string;
    filename: string;
    buffer: Buffer;
    password: string;
    replaceConfirmed: boolean;
  },
  dependencies: EvaluationDatabaseImportServiceDependencies = {},
): { run: EvaluationDatabaseImportRun } {
  assertProjectEvaluationDatabaseImportAvailable(input.projectId);
  if (!input.replaceConfirmed) {
    throw new Error('请先确认 Excel 中的同名表将替换目标数据库中的现有表');
  }

  const password = String(input.password ?? '');
  const config = loadEvaluationDatabaseConfig(input.projectId, password);
  validateEvaluationDatabaseExcelUpload(input.filename, input.buffer);
  const run = createEvaluationDatabaseImportRun({
    projectId: input.projectId,
    filename: input.filename,
    engine: config.engine,
    database: config.database,
  });
  const schedule = dependencies.schedule ?? scheduleBackgroundTask;
  try {
    schedule(() => runEvaluationDatabaseImportTask({
      run,
      filename: input.filename,
      buffer: input.buffer,
      password,
      config,
      dependencies,
    }));
  } catch (error) {
    finishEvaluationDatabaseImportRun(run.id, { error: safeError(error, password) });
    throw error;
  }
  return { run };
}

export function getProjectEvaluationDatabaseImports(
  projectId: string,
): EvaluationDatabaseImportRun[] {
  return listEvaluationDatabaseImportRuns(projectId);
}
