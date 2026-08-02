/**
 * 把长耗时操作从 HTTP 请求里挪出去。
 *
 * 同步跑在请求里的写操作，一个请求同时是租约持有者、续租心跳源、任务执行者和
 * 结果写入者——四者绑死在同一个 TCP 连接上。连接一断（关页面、网关超时），
 * 失败响应发不出去，界面就停在「正在处理」上。
 *
 * 这里统一收口后台作业的形状，免得每条路径各写一遍、各漏一处收尾：
 *
 * - 租约在作业里取、在作业里还。调用 startBackgroundJob 时 lease.acquire 已经
 *   同步执行完（async 函数体会一直跑到第一个 await），所以请求返回时租约已到手，
 *   不存在「返回了但还没锁住」的窗口。
 * - 失败必须自己兜住：那时候已经没有 HTTP 响应可以承载错误了，
 *   不写状态就会留下一个永远转圈的界面。
 */
import { runWithProjectLease, type ProjectOperationLease } from './lease.js';
import { OperationCancelledError, type InFlightOperations } from '../in-flight-operations.js';

export interface BackgroundJobOptions {
  jobs: InFlightOperations;
  leases: ProjectOperationLease;
  /** 去重键，同一项目同一类操作用同一个键 */
  key: string;
  operation: string;
  owner: string;
  ttlMs: number;
  projectId: string;
  run: () => Promise<unknown>;
  /** 失败收尾：把状态写成可重试，并给出用户能点的下一步 */
  onError: (reason: string) => void;
}

export function startBackgroundJob(options: BackgroundJobOptions): void {
  void options.jobs.run(options.key, options.operation, async () => {
    await runWithProjectLease(options.leases, {
      projectId: options.projectId,
      operation: options.operation,
      owner: options.owner,
      ttlMs: options.ttlMs,
    }, async () => {
      await options.run();
    });
  }).catch((error) => {
    const reason = error instanceof OperationCancelledError
      // 取消不是失败：用户自己按的，文案要说清楚是按他的要求停的，
      // 否则界面上会变成一条看不懂的报错，让人以为出了问题
      ? error.message
      : (error instanceof Error ? error.message : String(error));
    // 收尾自身再抛就没人接了，只能吞掉；重启恢复会兜住残留的 running 状态
    try { options.onError(reason); } catch { /* 收尾失败不再向上抛 */ }
  });
}
