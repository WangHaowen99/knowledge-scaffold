// 杀整棵进程树：先按 /proc 记下所有后代（有的子进程会自己换进程组），再杀进程组和每个后代。
import { readdirSync, readFileSync } from 'node:fs';

/** Linux 下从 /proc 找 pid 的全部后代；没有 /proc（macOS 跑单测）时返回空。 */
export function descendants(pid) {
  let entries;
  try {
    entries = readdirSync('/proc');
  } catch {
    return [];
  }
  const children = new Map();
  for (const entry of entries) {
    if (!/^\d+$/.test(entry)) continue;
    let stat;
    try {
      stat = readFileSync(`/proc/${entry}/stat`, 'utf8');
    } catch {
      continue;
    }
    // 第二列是 (comm)，里面可能有空格和括号，从最后一个 ) 往后数
    const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
    const parent = Number(fields[1]);
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(Number(entry));
  }
  const found = [];
  const queue = [pid];
  while (queue.length) {
    for (const child of children.get(queue.shift()) ?? []) {
      found.push(child);
      queue.push(child);
    }
  }
  return found;
}

export function killTree(pid, { group = false } = {}) {
  if (!pid) return;
  const pids = descendants(pid);
  if (group) {
    try {
      process.kill(-pid, 'SIGKILL');
    } catch {
      // 进程组已经没了
    }
  }
  for (const target of [pid, ...pids]) {
    try {
      process.kill(target, 'SIGKILL');
    } catch {
      // 已经退出
    }
  }
}
