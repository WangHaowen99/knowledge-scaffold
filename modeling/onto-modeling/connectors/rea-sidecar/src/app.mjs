// rea-sidecar 的 HTTP 接口（evidence-collection.md §5）。只在 rea 内部网络上，网络上只有网关，不验票据。
// 错误一律回 {"error": 代码, "message": 说明}，网关按这个形状转成自己的错误。
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdir, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import { Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

import { HttpError, JOB_ID, JobManager, TERMINAL } from './jobs.mjs';

const SOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const MAX_SECONDS = 6 * 3600;
const MAX_RECORDS = 10_000_000;
const LINGER_BYTES = 64 * 1024 * 1024;
const LINGER_MS = 10_000;

function sendJson(res, status, body, headers = {}) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': data.length, ...headers });
  res.end(data);
}

const sendError = (res, error, headers) => sendJson(res, error.status, { error: error.code, message: error.message }, headers);

async function sendFile(res, path, contentType) {
  const info = await stat(path).catch(() => null);
  if (!info) throw new HttpError(404, 'not_found', '文件不在了');
  res.writeHead(200, { 'Content-Type': contentType, 'Content-Length': info.size });
  await pipeline(createReadStream(path), res);
}

function integer(value, { fallback, min, max, name }) {
  if (value === null || value === '') return fallback;
  if (!/^\d+$/.test(value)) throw new HttpError(400, 'invalid_parameter', `${name} 要是正整数`);
  const number = Number(value);
  if (number < min || number > max) throw new HttpError(400, 'invalid_parameter', `${name} 超出范围`);
  return number;
}

/** 落盘用的文件名：只留字母数字和 ._-，保留扩展名；显示用的名字另存原样（去掉路径和控制字符）。 */
export function safeFilename(name) {
  const base = String(name ?? '').split(/[\\/]/).pop();
  const safe = base.replace(/[^A-Za-z0-9._-]/g, '_').replace(/^[._]+/, '').slice(-100);
  return safe || 'artifact';
}

const displayName = (name) => String(name ?? '').split(/[\\/]/).pop().replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 255)
  || 'artifact';

export function createApp(config) {
  const manager = new JobManager(config);

  async function intake(req, res, url) {
    const query = url.searchParams;
    const id = query.get('id') ?? '';
    const source = query.get('source') ?? '';
    const sha256 = (query.get('sha256') ?? '').toLowerCase();
    if (!JOB_ID.test(id)) throw new HttpError(400, 'invalid_parameter', 'id 不合规');
    if (!SOURCE_ID.test(source)) throw new HttpError(400, 'invalid_parameter', 'source 不合规');
    if (!SHA256.test(sha256)) throw new HttpError(400, 'invalid_parameter', 'sha256 不合规');
    const maxBytes = Math.min(
      integer(query.get('max_bytes'), { fallback: config.maxBytes, min: 1, max: Number.MAX_SAFE_INTEGER, name: 'max_bytes' }),
      config.maxBytes,
    );
    const maxSeconds = integer(query.get('max_seconds'), { fallback: 1800, min: 1, max: MAX_SECONDS, name: 'max_seconds' });
    const maxRecords = integer(query.get('max_records'), { fallback: 200000, min: 1, max: MAX_RECORDS, name: 'max_records' });
    const lengthHeader = req.headers['content-length'];
    if (req.headers['transfer-encoding'] || !/^\d+$/.test(lengthHeader ?? '')) {
      throw new HttpError(411, 'length_required', '要带 Content-Length');
    }
    const length = Number(lengthHeader);
    if (length > maxBytes) throw new HttpError(413, 'too_large', `目标程序超过 ${maxBytes} 字节`);

    const job = manager.create({ id, source, filename: displayName(query.get('filename')), sha256, maxSeconds, maxRecords });
    const inputPath = join(job.dir, 'input', safeFilename(query.get('filename')));
    const hash = createHash('sha256');
    let received = 0;
    const meter = new Transform({
      transform(chunk, _encoding, callback) {
        received += chunk.length;
        if (received > maxBytes) {
          callback(new HttpError(413, 'too_large', `目标程序超过 ${maxBytes} 字节`));
          return;
        }
        hash.update(chunk);
        callback(null, chunk);
      },
    });
    try {
      await mkdir(join(job.dir, 'input'), { recursive: true });
      await pipeline(req, meter, createWriteStream(inputPath));
      if (job.state === 'cancelled') throw new HttpError(409, 'cancelled', '任务已取消');
      if (received !== length) throw new HttpError(400, 'body_incomplete', '请求体没收全');
      if (hash.digest('hex') !== sha256) throw new HttpError(422, 'artifact_mismatch', '目标程序的 sha256 对不上');
    } catch (error) {
      await manager.discard(job);
      throw error instanceof HttpError ? error : new HttpError(400, 'body_incomplete', '请求体没收全');
    }
    await manager.accept(job, { inputPath });
    sendJson(res, 202, { id });
  }

  async function route(req, res) {
    const url = new URL(req.url, 'http://sidecar');
    const parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent);
    if (req.method === 'GET' && url.pathname === '/health') {
      const status = config.reaVersion ? 'ok' : 'unavailable';
      sendJson(res, config.reaVersion ? 200 : 503, { status, rea_version: config.reaVersion, ...manager.stats() });
      return;
    }
    if (parts[0] !== 'v1' || parts[1] !== 'jobs') throw new HttpError(404, 'not_found', '没有这个接口');
    if (parts.length === 2 && req.method === 'POST') {
      await intake(req, res, url);
      return;
    }
    const job = parts[2] ? manager.get(parts[2]) : null;
    if (!job) throw new HttpError(404, 'job_not_found', '没有这个任务');
    const action = parts.slice(3).join('/');
    const ready = () => {
      if (job.state !== 'succeeded' && job.state !== 'partial') throw new HttpError(409, 'not_ready', '任务没有成功结束');
    };
    if (req.method === 'GET' && parts.length === 3) {
      sendJson(res, 200, manager.status(job));
    } else if (req.method === 'POST' && action === 'cancel') {
      manager.cancel(job);
      sendJson(res, 200, manager.status(job));
    } else if (req.method === 'DELETE' && parts.length === 3) {
      await manager.remove(job);
      sendJson(res, 200, { job_id: job.id, deleted: true });
    } else if (req.method === 'GET' && action === 'result') {
      ready();
      await sendFile(res, join(job.dir, 'out', 'result.json'), 'application/json; charset=utf-8');
    } else if (req.method === 'GET' && action === 'records') {
      ready();
      await sendFile(res, join(job.dir, 'out', 'records.jsonl'), 'application/x-ndjson; charset=utf-8');
    } else if (req.method === 'GET' && parts[3] === 'raw' && parts.length === 5) {
      ready();
      const entry = job.raw.find((item) => item.name === parts[4]);
      if (!entry) throw new HttpError(404, 'not_found', '没有这份原始结果');
      await sendFile(res, join(job.dir, 'raw', entry.name), entry.media_type);
    } else {
      throw new HttpError(405, 'method_not_allowed', '不支持的方法');
    }
  }

  const server = createServer((req, res) => {
    route(req, res).catch((error) => {
      const known = error instanceof HttpError ? error : new HttpError(500, 'internal_error', '旁路容器出错');
      if (res.headersSent) {
        res.destroy();
        return;
      }
      // 请求体还没读完就回错误（超限、排满）：先回复，再把请求体读完丢掉，对端发完就能收到回复；
      // 直接断开时对端还在写，内核回 RST，对端常常连这条回复都收不到。丢弃超过上限或超时才断开。
      if (!req.complete && !req.destroyed) {
        let drained = 0;
        const timer = setTimeout(() => req.destroy(), LINGER_MS);
        timer.unref();
        req.on('data', (chunk) => {
          drained += chunk.length;
          if (drained > LINGER_BYTES) req.destroy();
        });
        req.on('end', () => clearTimeout(timer));
        req.on('error', () => {});
        req.resume();
        sendError(res, known);
      } else {
        sendError(res, known);
      }
    });
  });
  // 几百 MB 的程序从网关转过来可能要几分钟，不用默认的 5 分钟请求超时
  server.requestTimeout = 0;
  server.headersTimeout = 60_000;

  const sweeper = setInterval(() => manager.sweep().catch(() => {}), config.sweepMs ?? 10 * 60_000);
  sweeper.unref();
  server.on('close', () => clearInterval(sweeper));
  return { server, manager, TERMINAL };
}
