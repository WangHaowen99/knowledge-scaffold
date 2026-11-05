// 任务表与队列：同时跑 1 个、最多排 config.maxQueued 个；每个任务一个 runner 子进程（独立进程组），
// 超时和取消都杀整组；结束后保留到网关 DELETE，没人取的 ttl 后自动清（evidence-collection.md §5）。
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { chmod, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import { killTree } from './proc.mjs';
import { childEnv } from './rea.mjs';

export const TERMINAL = new Set(['succeeded', 'partial', 'failed', 'cancelled']);
export const JOB_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const STATES_DONE_PHASE = { succeeded: '完成', partial: '完成', failed: '失败', cancelled: '已取消' };

export class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** REA 解出来的目录可能带只读权限，非 root 删不掉；先放开权限再删。 */
export async function removeDir(dir) {
  try {
    await rm(dir, { recursive: true, force: true });
    return;
  } catch {
    // 下面放开权限再试
  }
  const open = async (path) => {
    await chmod(path, 0o700).catch(() => {});
    const entries = await readdir(path, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) if (entry.isDirectory()) await open(join(path, entry.name));
  };
  await open(dir);
  await rm(dir, { recursive: true, force: true });
}

export class JobManager {
  constructor(config) {
    this.config = config;
    this.jobs = new Map();
    this.queue = [];
    this.running = null;
  }

  get(id) {
    return this.jobs.get(id) ?? null;
  }

  stats() {
    const queued = [...this.jobs.values()].filter((job) => job.state === 'queued').length;
    return { running: this.running ? 1 : 0, queued };
  }

  /** 收程序之前先占位：重复 409，排满 503。收的过程算「排队」，不另开状态。 */
  create({ id, source, filename, sha256, maxSeconds, maxRecords }) {
    if (this.jobs.has(id)) throw new HttpError(409, 'job_exists', '任务已存在');
    const active = [...this.jobs.values()].filter((job) => !TERMINAL.has(job.state)).length;
    if (active >= 1 + this.config.maxQueued) throw new HttpError(503, 'queue_full', '排队已满');
    const now = Date.now();
    const job = {
      id, source, filename, sha256, maxRecords, dir: join(this.config.scratch, id),
      state: 'queued', phase: '接收程序', done: 0, total: 0, unit: '', error_code: null, error_message: null,
      created: now, deadline: now + maxSeconds * 1000, finished: null, raw: [],
    };
    this.jobs.set(id, job);
    return job;
  }

  /** 程序收齐、哈希核对过：写 job.json，进队列，起总时限（从收下任务算起，和网关的时限口径一致）。 */
  async accept(job, { inputPath }) {
    if (job.state !== 'queued') return;
    const spec = {
      source: job.source, filename: job.filename, sha256: job.sha256, inputPath,
      maxRecords: job.maxRecords, reaBin: this.config.reaBin, reaVersion: this.config.reaVersion,
      provider: this.config.provider, maxOutputBytes: this.config.maxOutputBytes, maxEntries: this.config.maxEntries,
      maxInventory: this.config.maxInventory, maxAssemblies: this.config.maxAssemblies,
      maxPrograms: this.config.maxPrograms, maxPyc: this.config.maxPyc,
    };
    await writeFile(join(job.dir, 'job.json'), JSON.stringify(spec));
    job.phase = '排队';
    job.timer = setTimeout(() => this.expire(job), Math.max(0, job.deadline - Date.now()));
    this.queue.push(job);
    this.pump();
  }

  async discard(job) {
    clearTimeout(job.timer);
    this.queue = this.queue.filter((item) => item !== job);
    this.jobs.delete(job.id);
    await removeDir(job.dir);
  }

  status(job) {
    const { id, state, phase, done, total, unit, error_code, error_message } = job;
    return { job_id: id, state, phase, done, total, unit, error_code, error_message };
  }

  expire(job) {
    if (job.state === 'queued') {
      this.queue = this.queue.filter((item) => item !== job);
      this.finish(job, 'failed', 'timeout', '超过时限');
    } else if (job === this.running) {
      job.timedOut = true;
      killTree(job.pid, { group: true });
    }
  }

  cancel(job) {
    if (TERMINAL.has(job.state)) return;
    if (job === this.running) {
      job.cancelRequested = true;
      job.phase = '取消中';
      killTree(job.pid, { group: true });
      return;
    }
    this.queue = this.queue.filter((item) => item !== job);
    this.finish(job, 'cancelled');
  }

  /** DELETE：在跑的先杀，进程退出后再删目录；任务从表里去掉。 */
  async remove(job) {
    job.deleted = true;
    clearTimeout(job.timer);
    if (job === this.running) {
      killTree(job.pid, { group: true });
      return;
    }
    await this.discard(job);
  }

  pump() {
    if (this.running) return;
    const job = this.queue.shift();
    if (job) this.start(job);
  }

  start(job) {
    this.running = job;
    job.state = 'running';
    job.phase = '识别类型';
    const child = spawn(process.execPath, [`--max-old-space-size=${this.config.runnerHeapMb}`, this.config.runnerPath, job.dir], {
      detached: true,
      cwd: job.dir,
      env: childEnv({ home: join(job.dir, 'home'), tmp: join(job.dir, 'tmp') }),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    job.pid = child.pid;
    createInterface({ input: child.stdout }).on('line', (line) => this.progress(job, line));
    let stderrTail = '';
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (text) => {
      stderrTail = (stderrTail + text).slice(-8192);
    });
    child.on('error', () => {});
    child.on('close', (code, signal) => {
      // 先让出名额：收尾是异步的，这期间来的 DELETE 要按「不在跑」处理
      this.running = null;
      this.closed(job, code, signal, stderrTail).catch(() => {}).finally(() => this.pump());
    });
  }

  progress(job, line) {
    let data;
    try {
      data = JSON.parse(line)?.progress;
    } catch {
      return;
    }
    if (!data || job.cancelRequested || job.timedOut) return;
    const count = (value) => (Number.isSafeInteger(value) && value >= 0 ? value : 0);
    job.phase = String(data.phase ?? job.phase).slice(0, 40);
    job.done = count(data.done);
    job.total = count(data.total);
    job.unit = String(data.unit ?? '').slice(0, 16);
  }

  async closed(job, code, signal, stderrTail) {
    clearTimeout(job.timer);
    // runner 自己退了但它起的进程可能还在（换了进程组的也算），按进程组再清一遍
    killTree(job.pid, { group: true });
    if (job.deleted) {
      this.jobs.delete(job.id);
      await removeDir(job.dir);
      return;
    }
    if (job.cancelRequested) {
      this.finish(job, 'cancelled');
    } else if (job.timedOut) {
      this.finish(job, 'failed', 'timeout', '超过时限');
    } else {
      let outcome = null;
      try {
        outcome = JSON.parse(await readFile(join(job.dir, 'out', 'outcome.json'), 'utf8'));
      } catch {
        outcome = null;
      }
      if (!outcome || !TERMINAL.has(outcome.state)) {
        // 多半是内存超限被杀
        if (stderrTail) process.stderr.write(`任务 ${job.id} 的处理进程异常退出（${code ?? signal}）\n`);
        this.finish(job, 'failed', 'runner_crashed', '处理进程异常退出');
      } else {
        if (outcome.state !== 'failed') {
          try {
            job.raw = JSON.parse(await readFile(join(job.dir, 'out', 'result.json'), 'utf8')).raw ?? [];
          } catch {
            job.raw = [];
          }
        }
        this.finish(job, outcome.state, outcome.error_code ?? null, outcome.error_message ?? null);
      }
    }
    // 结果、记录、原始结果留着给网关取；目标程序和解出来的东西现在就删，省 /scratch
    for (const name of ['input', 'tmp', 'work', 'home']) await removeDir(join(job.dir, name));
  }

  finish(job, state, errorCode = null, errorMessage = null) {
    clearTimeout(job.timer);
    Object.assign(job, {
      state, phase: STATES_DONE_PHASE[state], error_code: errorCode, error_message: errorMessage, finished: Date.now(),
    });
    if (state !== 'succeeded' && state !== 'partial') job.done = job.total = 0;
  }

  /** 结束超过 ttl 没人取的任务清掉。 */
  async sweep(now = Date.now()) {
    for (const job of [...this.jobs.values()]) {
      if (TERMINAL.has(job.state) && job.finished !== null && now - job.finished > this.config.ttlMs) await this.remove(job);
    }
  }

  /** 启动时清掉上次留下的任务目录：任务表只在内存里，重启后这些目录没人认领。 */
  async cleanupScratch() {
    await mkdir(this.config.scratch, { recursive: true });
    for (const entry of await readdir(this.config.scratch)) {
      if (JOB_ID.test(entry)) await removeDir(join(this.config.scratch, entry));
    }
  }

  shutdown() {
    if (this.running) killTree(this.running.pid, { group: true });
  }
}
