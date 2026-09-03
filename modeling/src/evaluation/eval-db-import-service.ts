import { getProjectDataSource } from './data-source-repo.js';
import {
  importEvaluationDatabaseExcel,
  mergeEvaluationDatabaseExcelFiles,
  validateEvaluationDatabaseExcelUpload,
  type EvaluationDatabaseDependencies,
  type EvaluationDatabaseExcelFileInput,
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
import {
  redactEvaluationDatabaseError,
  resolveEvaluationDatabaseTarget,
  type EvaluationDatabaseTarget,
} from './evaluation-database-target.js';

const MAX_CONCURRENT_EVALUATION_DATABASE_IMPORTS = 1;
const NL2SQL_PROVIDERS = new Set(['nl2sql_template', 'nl2sql_http']);

export type EvaluationDatabaseImportTaskScheduler = (task: () => Promise<void>) => void;

export interface EvaluationDatabaseImportServiceDependencies
  extends EvaluationDatabaseDependencies {
  databaseUrl?: string;
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

function loadEvaluationDatabaseTarget(
  projectId: string,
  databaseUrl?: string,
): EvaluationDatabaseTarget {
  const dataSource = getProjectDataSource(projectId);
  if (dataSource?.mode !== 'connection'
    || !dataSource.provider
    || !NL2SQL_PROVIDERS.has(dataSource.provider)) {
    throw new Error('请先保存并测试 NL2SQL 数据源配置');
  }
  return resolveEvaluationDatabaseTarget(dataSource.config.scene, databaseUrl);
}

function normalizeEvaluationDatabaseImportInput(input: {
  filename?: string;
  buffer?: Buffer;
  files?: EvaluationDatabaseExcelFileInput[];
}): EvaluationDatabaseExcelFileInput {
  return mergeEvaluationDatabaseExcelFiles(input.files ?? [{
    filename: input.filename ?? '',
    buffer: input.buffer ?? Buffer.alloc(0),
  }]);
}

async function runEvaluationDatabaseImportTask(input: {
  run: EvaluationDatabaseImportRun;
  filename: string;
  buffer: Buffer;
  target: EvaluationDatabaseTarget;
  dependencies: EvaluationDatabaseImportServiceDependencies;
}): Promise<void> {
  try {
    const importInput = {
      filename: input.filename,
      buffer: input.buffer,
      target: input.target,
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
      error: redactEvaluationDatabaseError(error, input.target).slice(0, 2_000),
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
    filename?: string;
    buffer?: Buffer;
    files?: EvaluationDatabaseExcelFileInput[];
    replaceConfirmed: boolean;
    databaseUrl?: string;
  },
  dependencies: EvaluationDatabaseImportServiceDependencies = {},
): { run: EvaluationDatabaseImportRun } {
  assertProjectEvaluationDatabaseImportAvailable(input.projectId);
  if (!input.replaceConfirmed) {
    throw new Error('请先确认 Excel 中的同名表将替换目标数据库中的现有表');
  }

  const target = loadEvaluationDatabaseTarget(
    input.projectId,
    input.databaseUrl || dependencies.databaseUrl,
  );
  const upload = normalizeEvaluationDatabaseImportInput(input);
  validateEvaluationDatabaseExcelUpload(upload.filename, upload.buffer);
  const run = createEvaluationDatabaseImportRun({
    projectId: input.projectId,
    filename: upload.filename,
    database: target.database,
  });
  const schedule = dependencies.schedule ?? scheduleBackgroundTask;
  try {
    schedule(() => runEvaluationDatabaseImportTask({
      run,
      filename: upload.filename,
      buffer: upload.buffer,
      target,
      dependencies,
    }));
  } catch (error) {
    finishEvaluationDatabaseImportRun(run.id, {
      error: redactEvaluationDatabaseError(error, target).slice(0, 2_000),
    });
    throw error;
  }
  return { run };
}

export function getProjectEvaluationDatabaseImports(
  projectId: string,
): EvaluationDatabaseImportRun[] {
  return listEvaluationDatabaseImportRuns(projectId);
}
