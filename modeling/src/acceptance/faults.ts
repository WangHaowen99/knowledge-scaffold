import { config } from '../config.js';
import { getDb, nowIso } from '../db/index.js';

export const ACCEPTANCE_FAULTS = [
  ['F01', '下一次模型调用在首个可见输出前超时'],
  ['F02', '下一次模型调用在部分输出后空闲超时'],
  ['F03', '下一次 Core 写入在事务提交前暂时失败'],
  ['F04', '下一次 Change Set 已提交但响应丢失'],
  ['F05', '下一次只读资料子任务超时'],
  ['F06', '下一次方案包含一项未授权修改'],
  ['F07', '下一次多动作 Change Set 在最后一项前失败'],
  ['F08', '下一次计划漏掉必选任务并尝试确认'],
  ['F09', '下一次校验重复同一问题直到有限停止'],
  ['F10', '下一次候选预检返回三项独立问题'],
  ['F11', '下一次确认使用已经过期的候选'],
  ['F12', '下一轮脚本沙箱测试失败'],
  ['F13', '下一轮评测持续运行直到用户停止'],
  ['F14', '下一轮评测全部返回运行环境错误'],
  ['F15', '下一轮生成停在最后一项计划任务前'],
  ['F16', '下一轮生成在业务批次间等待继续或停止'],
  ['F17', '下一次助手消息在创建运行前可重试失败'],
  ['F18', '下一次知识包文件读取失败'],
] as const;

export type AcceptanceFaultId = typeof ACCEPTANCE_FAULTS[number][0];
export type AcceptanceFaultStatus = 'armed' | 'consumed' | 'closed';

export interface AcceptanceFaultView {
  id: AcceptanceFaultId;
  description: string;
  status: AcceptanceFaultStatus;
  consumedCount: number;
  effectCount: number;
  armedAt?: string;
  consumedAt?: string;
  closedAt?: string;
}

export interface AcceptanceFaultEffect extends AcceptanceFaultView {
  active: boolean;
  firstEffect: boolean;
  runId: string;
}

export interface AcceptanceToolInjection {
  statusCode: number;
  payload: Record<string, unknown>;
}

const faultDescriptions = new Map<string, string>(ACCEPTANCE_FAULTS);

export function acceptanceModeEnabled(): boolean {
  return config.acceptanceMode;
}

export function isAcceptanceFaultId(value: string): value is AcceptanceFaultId {
  return faultDescriptions.has(value);
}

function assertAcceptanceMode(): void {
  if (!acceptanceModeEnabled()) throw new Error('浏览器验收模式未启用');
}

function assertFaultId(value: string): asserts value is AcceptanceFaultId {
  if (!isAcceptanceFaultId(value)) throw new Error('验收故障编号不存在');
}

function viewFromRow(id: AcceptanceFaultId, row?: any): AcceptanceFaultView {
  return {
    id,
    description: faultDescriptions.get(id)!,
    status: row?.status === 'armed' || row?.status === 'consumed' ? row.status : 'closed',
    consumedCount: Number(row?.consumed_count ?? 0),
    effectCount: Number(row?.effect_count ?? 0),
    ...(row?.armed_at ? { armedAt: String(row.armed_at) } : {}),
    ...(row?.consumed_at ? { consumedAt: String(row.consumed_at) } : {}),
    ...(row?.closed_at ? { closedAt: String(row.closed_at) } : {}),
  };
}

export function listAcceptanceFaults(owner: string): AcceptanceFaultView[] {
  assertAcceptanceMode();
  const rows = getDb().prepare('SELECT * FROM acceptance_faults WHERE owner = ?').all(owner) as any[];
  const byId = new Map(rows.map((row) => [String(row.fault_id), row]));
  return ACCEPTANCE_FAULTS.map(([id]) => viewFromRow(id, byId.get(id)));
}

export function armAcceptanceFault(owner: string, rawId: string): AcceptanceFaultView {
  assertAcceptanceMode();
  assertFaultId(rawId);
  const now = nowIso();
  getDb().prepare(`INSERT INTO acceptance_faults (
      owner, fault_id, status, consumed_count, effect_count, active_run_id,
      armed_at, consumed_at, closed_at, updated_at
    ) VALUES (?,?,'armed',0,0,NULL,?,NULL,NULL,?)
    ON CONFLICT(owner, fault_id) DO UPDATE SET
      status = 'armed', effect_count = 0, active_run_id = NULL,
      armed_at = excluded.armed_at, consumed_at = NULL, closed_at = NULL,
      updated_at = excluded.updated_at`)
    .run(owner, rawId, now, now);
  const row = getDb().prepare('SELECT * FROM acceptance_faults WHERE owner = ? AND fault_id = ?')
    .get(owner, rawId);
  return viewFromRow(rawId, row);
}

export function closeAcceptanceFault(owner: string, rawId: string): AcceptanceFaultView {
  assertAcceptanceMode();
  assertFaultId(rawId);
  const now = nowIso();
  getDb().prepare(`INSERT INTO acceptance_faults (
      owner, fault_id, status, consumed_count, effect_count, active_run_id,
      armed_at, consumed_at, closed_at, updated_at
    ) VALUES (?,?,'closed',0,0,NULL,NULL,NULL,?,?)
    ON CONFLICT(owner, fault_id) DO UPDATE SET
      status = 'closed', active_run_id = NULL, closed_at = excluded.closed_at,
      updated_at = excluded.updated_at`)
    .run(owner, rawId, now, now);
  const row = getDb().prepare('SELECT * FROM acceptance_faults WHERE owner = ? AND fault_id = ?')
    .get(owner, rawId);
  return viewFromRow(rawId, row);
}

export function closeAllAcceptanceFaults(owner: string): AcceptanceFaultView[] {
  assertAcceptanceMode();
  const now = nowIso();
  getDb().prepare(`UPDATE acceptance_faults SET
      status = 'closed', active_run_id = NULL, closed_at = ?, updated_at = ?
      WHERE owner = ? AND status != 'closed'`)
    .run(now, now, owner);
  return listAcceptanceFaults(owner);
}

/**
 * Consume one browser-armed fault. With stickyRun=true, subsequent checks in the
 * same logical run remain active while the browser still shows only one consumption.
 */
export function takeAcceptanceFault(
  owner: string,
  rawId: string,
  runId: string,
  options: { stickyRun?: boolean } = {},
): AcceptanceFaultEffect | null {
  if (!acceptanceModeEnabled()) return null;
  assertFaultId(rawId);
  const db = getDb();
  const row = db.prepare('SELECT * FROM acceptance_faults WHERE owner = ? AND fault_id = ?')
    .get(owner, rawId) as any;
  if (!row) return null;
  const now = nowIso();
  if (row.status === 'armed') {
    const result = db.prepare(`UPDATE acceptance_faults SET
      status = 'consumed', consumed_count = consumed_count + 1, effect_count = 1,
      active_run_id = ?, consumed_at = ?, updated_at = ?
      WHERE owner = ? AND fault_id = ? AND status = 'armed'`)
      .run(runId, now, now, owner, rawId);
    if (Number(result.changes) !== 1) return null;
    const current = db.prepare('SELECT * FROM acceptance_faults WHERE owner = ? AND fault_id = ?')
      .get(owner, rawId);
    return { ...viewFromRow(rawId, current), active: true, firstEffect: true, runId };
  }
  if (options.stickyRun && row.status === 'consumed' && row.active_run_id === runId) {
    db.prepare(`UPDATE acceptance_faults SET effect_count = effect_count + 1, updated_at = ?
      WHERE owner = ? AND fault_id = ?`).run(now, owner, rawId);
    const current = db.prepare('SELECT * FROM acceptance_faults WHERE owner = ? AND fault_id = ?')
      .get(owner, rawId);
    return { ...viewFromRow(rawId, current), active: true, firstEffect: false, runId };
  }
  return null;
}

function toolFailure(
  statusCode: number,
  code: string,
  reason: string,
  next: string[],
  details: Record<string, unknown> = {},
): AcceptanceToolInjection {
  return {
    statusCode,
    payload: {
      code,
      error: code,
      reason,
      next,
      display: { text: reason },
      ...details,
    },
  };
}

export function acceptanceAgentToolBefore(
  owner: string,
  runId: string,
  tool: string,
): AcceptanceToolInjection | null {
  if (!acceptanceModeEnabled()) return null;
  if (tool === 'read_documents_fully' && takeAcceptanceFault(owner, 'F05', runId)) {
    return toolFailure(504, 'READ_TASK_TIMEOUT', '资料精读子任务等待超时，本次只读任务没有产生任何修改', ['从当前资料任务重试']);
  }
  if (tool === 'stage_change_set' && takeAcceptanceFault(owner, 'F03', runId)) {
    return toolFailure(503, 'CHANGE_SET_TEMPORARILY_UNAVAILABLE', '事务提交前遇到暂时故障，整批修改尚未写入，可以安全重试', ['使用同一变更要求重试当前 Change Set'], { retryable: true });
  }
  if (tool === 'stage_change_set' && takeAcceptanceFault(owner, 'F06', runId)) {
    return toolFailure(409, 'CHANGE_SET_OUT_OF_SCOPE', '实际动作超出已经批准的计划范围，整批修改已拒绝', ['删除计划外动作，或先提交计划增量并等待批准'], { retryable: false });
  }
  if (tool === 'stage_change_set' && takeAcceptanceFault(owner, 'F07', runId)) {
    return toolFailure(503, 'CHANGE_SET_ATOMIC_ROLLBACK', '最后一项应用前发生故障，当前 Change Set 已整批回滚', ['从当前业务批次重试'], { retryable: true, rolledBack: true });
  }
  if (tool === 'validate_change_set' && takeAcceptanceFault(owner, 'F08', runId)) {
    return toolFailure(409, 'CHANGE_PLAN_INCOMPLETE', '任务清单还有必选项未完成，不能提前生成整批确认', ['返回计划补齐未完成任务'], { retryable: false, incompleteTasks: ['必选任务 3'] });
  }
  if (tool === 'validate_change_set') {
    const repeated = takeAcceptanceFault(owner, 'F09', runId, { stickyRun: true });
    if (repeated) {
      const exhausted = repeated.effectCount >= 2;
      return toolFailure(
        exhausted ? 422 : 409,
        exhausted ? 'VALIDATION_RETRY_EXHAUSTED' : 'CANDIDATE_VALIDATION_FAILED',
        exhausted
          ? '相同校验问题重试后仍未变化，本轮已停止自动重试'
          : `候选仍有同一项校验问题（${repeated.effectCount}/2）`,
        exhausted ? ['修改需求后重试', '放弃当前候选'] : ['修正当前问题后重新校验'],
        {
          retryable: !exhausted,
          findingCount: 1,
          findings: [{ fingerprint: 'acceptance-repeated-finding', path: 'rules/discount_eligibility.md', message: '合成重复校验问题' }],
        },
      );
    }
    if (takeAcceptanceFault(owner, 'F10', runId)) {
      return toolFailure(409, 'CANDIDATE_VALIDATION_FAILED', '候选预检发现 3 项需要分别处理的问题', ['逐项修正全部问题后重新预检'], {
        retryable: true,
        findingCount: 3,
        findings: [
          { fingerprint: 'acceptance-finding-1', path: 'objects/member_profile.md', message: '合成对象边界需要补充' },
          { fingerprint: 'acceptance-finding-2', path: 'rules/discount_eligibility.md', message: '合成规则例外需要补充' },
          { fingerprint: 'acceptance-finding-3', path: 'metrics/review_pass_rate.md', message: '合成指标空值边界需要补充' },
        ],
      });
    }
    if (takeAcceptanceFault(owner, 'F15', runId)) {
      return toolFailure(409, 'CHANGE_PLAN_INCOMPLETE', '最后一项指标任务尚未完成，当前阶段不能进入验证', ['完成计划中的最后一项指标任务'], {
        retryable: false,
        incompleteTasks: ['生成复核通过率指标'],
      });
    }
  }
  if (tool === 'request_confirmation' && takeAcceptanceFault(owner, 'F11', runId)) {
    return toolFailure(409, 'CANDIDATE_REVALIDATION_REQUIRED', '待确认候选已经变化，原确认不能继续提交', ['重新执行完整候选预检，并基于新的确认卡再次决定'], { retryable: false });
  }
  if (tool === 'run_script_test' && takeAcceptanceFault(owner, 'F12', runId)) {
    return {
      statusCode: 200,
      payload: {
        ok: false,
        passed: 0,
        failed: 2,
        total: 2,
        sandbox: 'JiuwenBox',
        results: [
          { name: '1+2', status: 'failed', expected: { total: 3 }, actual: { error: '合成沙箱失败' } },
          { name: '0+0', status: 'failed', expected: { total: 0 }, actual: { error: '合成沙箱失败' } },
        ],
        display: { text: '脚本沙箱测试失败，当前源码不能进入确认' },
      },
    };
  }
  return null;
}

export function acceptanceAgentToolAfter(
  owner: string,
  runId: string,
  tool: string,
): AcceptanceToolInjection | null {
  if (tool !== 'stage_change_set' || !takeAcceptanceFault(owner, 'F04', runId)) return null;
  return toolFailure(
    503,
    'CHANGE_SET_RESULT_UNKNOWN',
    'Change Set 已提交，但返回助手的响应丢失；重试前必须先查询当前回执',
    ['使用原变更标识查询回执，不要重复创建候选'],
    { retryable: true, committed: true },
  );
}
