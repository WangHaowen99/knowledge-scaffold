import { parentPort } from 'node:worker_threads';
import { importEvaluationDatabaseExcel } from './eval-db.js';
import {
  redactEvaluationDatabaseError,
  type EvaluationDatabaseTarget,
} from './evaluation-database-target.js';

interface ImportWorkerRequest {
  filename: string;
  buffer: ArrayBuffer;
  target: EvaluationDatabaseTarget;
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
        target: request.target,
      }),
    };
  } catch (error) {
    response = { error: redactEvaluationDatabaseError(error, request.target) };
  }
  workerPort.postMessage(response);
  workerPort.close();
});
