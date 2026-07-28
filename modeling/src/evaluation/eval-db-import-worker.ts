import { parentPort } from 'node:worker_threads';
import type { EvaluationDatabaseConfig } from './data-source.js';
import { importEvaluationDatabaseExcel } from './eval-db.js';

interface ImportWorkerRequest {
  filename: string;
  buffer: ArrayBuffer;
  config: EvaluationDatabaseConfig;
}

interface ImportWorkerResponse {
  result?: Awaited<ReturnType<typeof importEvaluationDatabaseExcel>>;
  error?: string;
}

const workerPort = parentPort;
if (!workerPort) throw new Error('评测库导入 Worker 缺少父进程端口');

workerPort.once('message', async (request: ImportWorkerRequest) => {
  let response: ImportWorkerResponse;
  try {
    response = {
      result: await importEvaluationDatabaseExcel({
        filename: request.filename,
        buffer: Buffer.from(request.buffer),
        config: request.config,
      }),
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    response = {
      error: request.config.password
        ? message.split(request.config.password).join('[REDACTED]')
        : message,
    };
  }
  workerPort.postMessage(response);
  workerPort.close();
});
