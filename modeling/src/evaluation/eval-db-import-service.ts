import { getProjectDataSource } from './data-source-repo.js';
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

export class EvaluationDatabaseImportRequestError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly statusCode: 400 | 409 | 503,
    readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'EvaluationDatabaseImportRequestError';
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
    throw new EvaluationDatabaseImportRequestError(
      'EVALUATION_DATA_SOURCE_REQUIRED',
      '请先保存并测试 NL2SQL 数据源配置',
      409,
    );
  }
  try {
    return resolveEvaluationDatabaseTarget(dataSource.config.scene, databaseUrl);
  } catch (error) {
    throw new EvaluationDatabaseImportRequestError(
      'EVAL_DB_ADMIN_UNAVAILABLE',
      '评测数据库配置暂时不可用，请稍后重试',
      503,
      error,
    );
  }
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
    filename: string;
    buffer: Buffer;
    replaceConfirmed: boolean;
    databaseUrl?: string;
  },
  dependencies: EvaluationDatabaseImportServiceDependencies = {},
): { run: EvaluationDatabaseImportRun } {
  assertProjectEvaluationDatabaseImportAvailable(input.projectId);
  if (!input.replaceConfirmed) {
    throw new EvaluationDatabaseImportRequestError(
      'EVALUATION_DATABASE_IMPORT_CONFIRMATION_REQUIRED',
      '请先确认 Excel 中的同名表将替换目标数据库中的现有表',
      400,
    );
  }

  const target = loadEvaluationDatabaseTarget(
    input.projectId,
    input.databaseUrl || dependencies.databaseUrl,
  );
  try {
    validateEvaluationDatabaseExcelUpload(input.filename, input.buffer);
  } catch (error) {
    throw new EvaluationDatabaseImportRequestError(
      'EVALUATION_DATABASE_IMPORT_INVALID',
      error instanceof Error ? error.message : 'Excel 文件不正确',
      400,
      error,
    );
  }
  const run = createEvaluationDatabaseImportRun({
    projectId: input.projectId,
    filename: input.filename,
    database: target.database,
  });
  const schedule = dependencies.schedule ?? scheduleBackgroundTask;
  try {
    schedule(() => runEvaluationDatabaseImportTask({
      run,
      filename: input.filename,
      buffer: input.buffer,
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
