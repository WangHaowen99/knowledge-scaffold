import { Worker } from 'node:worker_threads';
import type { EvaluationDatabaseConfig } from './data-source.js';
import type { EvaluationDatabaseImportResult } from './eval-db.js';

interface ImportWorkerResponse {
  result?: EvaluationDatabaseImportResult;
  error?: string;
}

function safeError(error: unknown, password: string): string {
  const message = error instanceof Error ? error.message : String(error);
  return password ? message.split(password).join('[REDACTED]') : message;
}

export function importEvaluationDatabaseExcelInWorker(input: {
  filename: string;
  buffer: Buffer;
  config: EvaluationDatabaseConfig;
}): Promise<EvaluationDatabaseImportResult> {
  const extension = import.meta.url.endsWith('.ts') ? 'ts' : 'js';
  const workerModuleUrl = new URL(`./eval-db-import-worker.${extension}`, import.meta.url);
  const workerEntry = extension === 'ts'
    ? new URL(`data:text/javascript,${encodeURIComponent([
        `import { tsImport } from ${JSON.stringify(import.meta.resolve('tsx/esm/api'))};`,
        `await tsImport(${JSON.stringify(workerModuleUrl.href)}, ${JSON.stringify(workerModuleUrl.href)});`,
      ].join('\n'))}`)
    : workerModuleUrl;
  const worker = new Worker(
    workerEntry,
    {
      name: 'evaluation-database-import',
      resourceLimits: { maxOldGenerationSizeMb: 512 },
      // `node --input-type=module` 仅适用于 stdin/eval，继承到文件型 Worker 会启动失败。
      execArgv: process.execArgv.filter((argument) => !argument.startsWith('--input-type')),
    },
  );
  const transferable = Uint8Array.from(input.buffer).buffer;

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (outcome: { result: EvaluationDatabaseImportResult } | { error: unknown }) => {
      if (settled) return;
      settled = true;
      void worker.terminate();
      if ('result' in outcome) resolve(outcome.result);
      else reject(new Error(safeError(outcome.error, input.config.password)));
    };
    worker.once('message', (message: ImportWorkerResponse) => {
      if (message.result) finish({ result: message.result });
      else finish({ error: message.error || '评测库导入 Worker 未返回结果' });
    });
    worker.once('error', (error) => finish({ error }));
    worker.once('exit', (code) => {
      if (!settled) {
        finish({
          error: code === 0
            ? '评测库导入 Worker 已退出但未返回结果'
            : `评测库导入 Worker 异常退出（code=${code}）`,
        });
      }
    });
    worker.postMessage({
      filename: input.filename,
      buffer: transferable,
      config: input.config,
    }, [transferable]);
  });
}
