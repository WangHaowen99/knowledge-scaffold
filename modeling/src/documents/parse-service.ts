import { Worker } from 'node:worker_threads';

import {
  parseDocument,
  parseFullTableDocument,
  type DocKind,
  type FullTableReadResult,
  type ParseResult,
} from './parser.js';

const DEFAULT_PARSE_TIMEOUT_MS = 10 * 60_000;

interface ParseWorkerMessage {
  ok: boolean;
  result?: ParseResult | FullTableReadResult;
  error?: string;
}

export function parseDocumentIsolated(
  kind: DocKind,
  buffer: Buffer,
  filename: string,
  timeoutMs = DEFAULT_PARSE_TIMEOUT_MS,
): Promise<ParseResult> {
  // tsx 源码模式不会把 Worker 内的 .js specifier 映射回 .ts；生产构建始终走隔离 Worker。
  if (import.meta.url.endsWith('.ts')) return parseDocument(kind, buffer, filename);
  return new Promise((resolve, reject) => {
    const bytes = Uint8Array.from(buffer);
    const worker = new Worker(new URL('./parse-worker.js', import.meta.url), {
      workerData: { operation: 'parse', kind, filename, bytes },
      transferList: [bytes.buffer],
    });
    let settled = false;
    const finish = (error?: Error, result?: ParseResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      void worker.terminate();
      if (error) reject(error);
      else resolve(result ?? { error: '解析进程没有返回结果' });
    };
    const timer = setTimeout(() => {
      finish(new Error(`资料解析超过 ${Math.ceil(timeoutMs / 60_000)} 分钟`));
    }, timeoutMs);
    timer.unref();
    worker.once('message', (message: ParseWorkerMessage) => {
      if (message.ok) finish(undefined, message.result);
      else finish(new Error(message.error || '资料解析进程失败'));
    });
    worker.once('error', (error) => finish(error));
    worker.once('exit', (code) => {
      if (!settled && code !== 0) finish(new Error(`资料解析进程异常退出（${code}）`));
    });
  });
}

export function parseFullTableDocumentIsolated(
  kind: Extract<DocKind, 'excel' | 'csv'>,
  buffer: Buffer,
  filename: string,
  timeoutMs = DEFAULT_PARSE_TIMEOUT_MS,
): Promise<FullTableReadResult> {
  if (import.meta.url.endsWith('.ts')) return Promise.resolve(parseFullTableDocument(kind, buffer, filename));
  return new Promise((resolve, reject) => {
    const bytes = Uint8Array.from(buffer);
    const worker = new Worker(new URL('./parse-worker.js', import.meta.url), {
      workerData: { operation: 'full-table', kind, filename, bytes },
      transferList: [bytes.buffer],
    });
    let settled = false;
    const finish = (error?: Error, result?: FullTableReadResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      void worker.terminate();
      if (error) reject(error);
      else if (result) resolve(result);
      else reject(new Error('表格全文展开进程没有返回结果'));
    };
    const timer = setTimeout(() => finish(new Error(
      `表格全文展开超过 ${Math.ceil(timeoutMs / 60_000)} 分钟`,
    )), timeoutMs);
    timer.unref();
    worker.once('message', (message: ParseWorkerMessage) => {
      if (message.ok) finish(undefined, message.result as FullTableReadResult);
      else finish(new Error(message.error || '表格全文展开进程失败'));
    });
    worker.once('error', (error) => finish(error));
    worker.once('exit', (code) => {
      if (!settled && code !== 0) finish(new Error(`表格全文展开进程异常退出（${code}）`));
    });
  });
}
