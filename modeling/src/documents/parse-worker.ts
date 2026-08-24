import { parentPort, workerData } from 'node:worker_threads';

import { parseDocument, parseFullTableDocument, type DocKind } from './parser.js';

interface ParseWorkerInput {
  operation?: 'parse' | 'full-table';
  kind: DocKind;
  filename: string;
  bytes: Uint8Array;
}

const input = workerData as ParseWorkerInput;

const task = input.operation === 'full-table'
  ? Promise.resolve(parseFullTableDocument(
      input.kind as Extract<DocKind, 'excel' | 'csv'>,
      Buffer.from(input.bytes),
      input.filename,
    ))
  : parseDocument(input.kind, Buffer.from(input.bytes), input.filename);

void task
  .then((result) => parentPort?.postMessage({ ok: true, result }))
  .catch((error) => parentPort?.postMessage({
    ok: false,
    error: error instanceof Error ? error.message : String(error),
  }));
