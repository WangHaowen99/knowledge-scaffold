// 跑 REA 子进程：只放白名单里的静态命令（evidence-collection.md §5），环境变量只给 PATH、HOME、LANG、TMPDIR。
import { spawn } from 'node:child_process';
import { createWriteStream } from 'node:fs';

import { killTree } from './proc.mjs';

export const WHITELIST = new Set([
  'inspect-artifact', 'extract-artifact', 'inspect-desktop-application', 'analyze-javascript-application',
  'inspect-managed-artifact', 'inspect-managed-members', 'inspect-managed-native-boundaries',
  'project-managed-application-graph', 'inspect-python-pyc', 'inspect-pe-imports', 'inspect-electron-fuses',
  'capabilities', 'doctor',
]);

export class ReaError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

const STDERR_TAIL = 16 * 1024;

/**
 * 子进程环境：HOME、TMPDIR 都在任务目录里（REA 的 extract-artifact 解到 os.tmpdir()，跟着任务目录一起清），
 * 不继承父进程的任何变量，代理、密钥之类带不进去。
 */
export function childEnv({ home, tmp }) {
  return { PATH: '/usr/local/bin:/usr/bin:/bin', HOME: home, LANG: 'C.UTF-8', TMPDIR: tmp };
}

/** `(3/6)` 这类进度写在 REA 的 message 里；只取数字，不转发 message（里面有目标程序的文件名）。 */
export function parseProgress(line) {
  let data;
  try {
    data = JSON.parse(line);
  } catch {
    return null;
  }
  const progress = data?.rea_progress;
  if (!progress || typeof progress !== 'object') return null;
  const match = /\((\d+)\/(\d+)\)/.exec(String(progress.message ?? ''));
  if (match) return { done: Number(match[1]), total: Number(match[2]), unit: '文件' };
  const done = Number.isSafeInteger(progress.completed) ? progress.completed : 0;
  const total = Number.isSafeInteger(progress.total) ? progress.total : 0;
  return { done, total, unit: '步' };
}

/**
 * 跑一条 REA 命令，stdout 落到 stdoutPath。子进程不 detach，和 runner 同一个进程组，取消时一起杀。
 * 输出超过 maxOutputBytes 立即杀掉并报 output_too_large。
 */
export function runRea(command, args, { reaBin, cwd, env, stdoutPath, maxOutputBytes, onProgress = () => {} }) {
  if (!WHITELIST.has(command)) return Promise.reject(new ReaError('command_not_allowed', `命令不在白名单：${command}`));
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const out = createWriteStream(stdoutPath);
    const child = spawn(process.execPath, [reaBin, command, ...args, '--format', 'json'], {
      cwd, env, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let bytes = 0;
    let tooLarge = false;
    let stderrTail = '';
    let pending = '';
    child.stdout.on('data', (chunk) => {
      bytes += chunk.length;
      if (bytes > maxOutputBytes) {
        if (!tooLarge) {
          tooLarge = true;
          killTree(child.pid);
        }
        return;
      }
      out.write(chunk);
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (text) => {
      pending += text;
      const lines = pending.split('\n');
      pending = lines.pop();
      if (pending.length > STDERR_TAIL) pending = pending.slice(-STDERR_TAIL);
      for (const line of lines) {
        const progress = parseProgress(line);
        if (progress) onProgress(progress);
        else if (line.trim()) stderrTail = (stderrTail + line + '\n').slice(-STDERR_TAIL);
      }
    });
    child.on('error', (error) => {
      out.end();
      reject(new ReaError('spawn_failed', error.message));
    });
    child.on('close', (code, signal) => {
      out.end(() => {
        const ms = Date.now() - started;
        if (tooLarge) reject(new ReaError('output_too_large', `${command} 的输出超过 ${maxOutputBytes} 字节`));
        else resolve({ code, signal, ms, bytes, stderrTail });
      });
    });
  });
}
