import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import { assistantActionKind, type AssistantActionKind } from './assistant-actions.js';
import type { JsonObject } from '../authoring/types.js';
import type { AiConstructionTaskGraphSnapshot } from './task-graph.js';
import { AI_CONSTRUCTION_TEXT_BLOCK_MAX_LENGTH } from './text-blocks.js';

export const MAX_CHANGE_PLAN_DESCRIPTION_LENGTH = 6_000;

export type AiConstructionMessageRole = 'assistant' | 'user' | 'system';

export interface AiConstructionStatusBlock {
  type: 'status';
  title: string;
  summary?: string;
  milestoneId?: string;
  taskId?: string;
  progress?: number;
  eta?: {
    minMinutes: number | null;
    maxMinutes: number | null;
    confidence: 'low' | 'medium' | 'high';
  };
}

export interface AiConstructionTextBlock {
  type: 'text';
  text: string;
}

export interface AiConstructionEvidenceBlock {
  type: 'evidence';
  title: string;
  items: Array<{ source: string; summary: string; locator?: string; path?: string }>;
}

export interface AiConstructionValidationBlock {
  type: 'validation';
  title: string;
  scope: 'node' | 'incremental' | 'milestone' | 'complete' | 'release';
  counts: { errors: number; warnings: number; hints: number };
  findings?: Array<{
    severity: 'error' | 'warning' | 'hint';
    title: string;
    summary: string;
    target?: string;
  }>;
}

export interface AiConstructionTaskPlanBlock {
  type: 'task_plan';
  title: string;
  note?: string;
  tasks: Array<{
    id: string;
    title: string;
    status: 'pending' | 'ready' | 'running' | 'waiting_user' | 'waiting_confirmation'
      | 'completed' | 'failed' | 'skipped' | 'stale' | 'cancelled';
    required: boolean;
    reason?: string;
  }>;
}

export interface AiConstructionChangePlanBlock {
  type: 'change_plan';
  interactionId: string;
  title: string;
  summary: string;
  status: 'pending' | 'approved' | 'applied' | 'failed' | 'rejected';
  changes: Array<{
    id: string;
    targetKind: 'object' | 'operation' | 'rule' | 'metric' | 'script';
    targetKey: string;
    action: 'add' | 'modify' | 'remove';
    description: string;
  }>;
  risks?: string[];
}

export interface AiConstructionNextActionsBlock {
  type: 'next_actions';
  title?: string;
  actions: Array<{
    id: string;
    /** 由 assistant-actions 目录声明；前端据此分发，不再按 id 猜语义 */
    kind?: AssistantActionKind;
    label: string;
    description?: string;
    /** 仅 open_package_file 使用：知识包内相对路径或可解析的资源语义目标 */
    path?: string;
    style: 'primary' | 'secondary' | 'danger';
  }>;
}

export type AiConstructionMessageBlock =
  | AiConstructionStatusBlock
  | AiConstructionTextBlock
  | AiConstructionEvidenceBlock
  | AiConstructionValidationBlock
  | AiConstructionTaskPlanBlock
  | AiConstructionChangePlanBlock
  | AiConstructionNextActionsBlock;

export interface AiConstructionMessage {
  id: string;
  projectId: string;
  taskId?: string;
  key?: string;
  seq: number;
  role: AiConstructionMessageRole;
  blocks: AiConstructionMessageBlock[];
  createdAt: string;
}

export interface AppendAiConstructionMessageInput {
  key?: string;
  taskId?: string;
  role: AiConstructionMessageRole;
  blocks: AiConstructionMessageBlock[];
  /**
   * 纯进度刷新：前端贴上消息即可，无需回源重载 session。
   * 只有确实不改变任务图的写入才能标记——伴随任务状态变化的消息若被标记，
   * 前端会一直拿着过期的任务状态（历史上「分析中按钮不置灰」就是这么来的）。
   */
  progressOnly?: boolean;
}

export interface AiConstructionMessagePage {
  messages: AiConstructionMessage[];
  hasMore: boolean;
  oldestSeq: number | null;
  newestSeq: number | null;
}

export type AiConstructionMessageStoreErrorCode =
  | 'PROJECT_NOT_FOUND'
  | 'TASK_NOT_FOUND'
  | 'INVALID_MESSAGE'
  | 'INVALID_BLOCK';

export class AiConstructionMessageStoreError extends Error {
  constructor(
    public readonly code: AiConstructionMessageStoreErrorCode,
    message: string,
  ) {
    super(message);
    this.name = 'AiConstructionMessageStoreError';
  }
}

const BLOCK_ID_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/;
// 知识包内的相对路径或 object:key 等语义目标。拒绝绝对路径和 .. 段，避免导向包外。
const PACKAGE_PATH_PATTERN = /^[A-Za-z0-9_.:-]+(\/[A-Za-z0-9_.:-]+)*$/;
const MESSAGE_ROLES = new Set<AiConstructionMessageRole>(['assistant', 'user', 'system']);
const ACTION_STYLES = new Set(['primary', 'secondary', 'danger']);
const CONFIDENCE = new Set(['low', 'medium', 'high']);
const VALIDATION_SCOPES = new Set(['node', 'incremental', 'milestone', 'complete', 'release']);
const FINDING_SEVERITIES = new Set(['error', 'warning', 'hint']);
const TASK_PLAN_STATUSES = new Set([
  'pending', 'ready', 'running', 'waiting_user', 'waiting_confirmation',
  'completed', 'failed', 'skipped', 'stale', 'cancelled',
]);
const CHANGE_PLAN_STATUSES = new Set(['pending', 'approved', 'applied', 'failed', 'rejected']);
const CHANGE_TARGET_KINDS = new Set(['object', 'operation', 'rule', 'metric', 'script']);
const CHANGE_ACTIONS = new Set(['add', 'modify', 'remove']);

function objectValue(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new AiConstructionMessageStoreError('INVALID_BLOCK', '消息块格式不正确');
  }
  return value as Record<string, unknown>;
}

function textValue(value: unknown, label: string, max: number): string {
  const text = typeof value === 'string' ? value.trim() : '';
  if (!text || text.length > max) {
    throw new AiConstructionMessageStoreError('INVALID_BLOCK', `${label}不能为空且不能超过 ${max} 字`);
  }
  return text;
}

function textBlockValue(value: unknown): string {
  const text = typeof value === 'string' ? value : '';
  if (!text.trim() || text.length > AI_CONSTRUCTION_TEXT_BLOCK_MAX_LENGTH) {
    throw new AiConstructionMessageStoreError(
      'INVALID_BLOCK',
      `说明内容不能为空且不能超过 ${AI_CONSTRUCTION_TEXT_BLOCK_MAX_LENGTH} 字`,
    );
  }
  return text;
}

function optionalText(value: unknown, label: string, max: number): string | undefined {
  if (value == null || value === '') return undefined;
  return textValue(value, label, max);
}

function nullableMinutes(value: unknown, label: string): number | null {
  if (value == null) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0 || number > 7 * 24 * 60) {
    throw new AiConstructionMessageStoreError('INVALID_BLOCK', `${label}不正确`);
  }
  return number;
}

function countValue(value: unknown, label: string): number {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0 || number > 100_000) {
    throw new AiConstructionMessageStoreError('INVALID_BLOCK', `${label}不正确`);
  }
  return number;
}

function normalizeBlock(value: unknown): AiConstructionMessageBlock {
  const block = objectValue(value);
  if (block.type === 'status') {
    const progress = block.progress == null ? undefined : Number(block.progress);
    if (progress !== undefined && (!Number.isFinite(progress) || progress < 0 || progress > 100)) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '进度必须在 0 到 100 之间');
    }
    let eta: AiConstructionStatusBlock['eta'];
    if (block.eta != null) {
      const rawEta = objectValue(block.eta);
      if (!CONFIDENCE.has(String(rawEta.confidence))) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', 'ETA 可信度不正确');
      }
      eta = {
        minMinutes: nullableMinutes(rawEta.minMinutes, '最短时长'),
        maxMinutes: nullableMinutes(rawEta.maxMinutes, '最长时长'),
        confidence: String(rawEta.confidence) as 'low' | 'medium' | 'high',
      };
      if (eta.minMinutes !== null && eta.maxMinutes !== null && eta.minMinutes > eta.maxMinutes) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', 'ETA 最短时长不能大于最长时长');
      }
    }
    const summary = optionalText(block.summary, '状态说明', 500);
    const milestoneId = optionalText(block.milestoneId, '阶段', 100);
    const taskId = optionalText(block.taskId, '任务', 100);
    return {
      type: 'status',
      title: textValue(block.title, '状态标题', 100),
      ...(summary ? { summary } : {}),
      ...(milestoneId ? { milestoneId } : {}),
      ...(taskId ? { taskId } : {}),
      ...(progress === undefined ? {} : { progress }),
      ...(eta ? { eta } : {}),
    };
  }
  if (block.type === 'text') {
    return {
      type: 'text',
      text: textBlockValue(block.text),
    };
  }
  if (block.type === 'evidence') {
    if (!Array.isArray(block.items) || block.items.length < 1 || block.items.length > 20) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '证据卡必须包含 1 到 20 条证据');
    }
    const items = block.items.map((value) => {
      const item = objectValue(value);
      const locator = optionalText(item.locator, '证据定位', 300);
      const path = optionalText(item.path, '证据文件', 300);
      return {
        source: textValue(item.source, '证据来源', 300),
        summary: textValue(item.summary, '证据摘要', 1_000),
        ...(locator ? { locator } : {}),
        ...(path ? { path } : {}),
      };
    });
    return { type: 'evidence', title: textValue(block.title, '证据标题', 100), items };
  }
  if (block.type === 'validation') {
    const scope = String(block.scope ?? '');
    if (!VALIDATION_SCOPES.has(scope)) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '校验范围不正确');
    }
    const rawCounts = objectValue(block.counts);
    const counts = {
      errors: countValue(rawCounts.errors, '错误数'),
      warnings: countValue(rawCounts.warnings, '警告数'),
      hints: countValue(rawCounts.hints, '提示数'),
    };
    if (block.findings != null && !Array.isArray(block.findings)) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '校验发现格式不正确');
    }
    const findings = (block.findings ?? []).slice(0, 20).map((value: unknown) => {
      const finding = objectValue(value);
      const severity = String(finding.severity ?? '');
      if (!FINDING_SEVERITIES.has(severity)) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', '校验发现级别不正确');
      }
      const target = optionalText(finding.target, '发现位置', 300);
      return {
        severity: severity as 'error' | 'warning' | 'hint',
        title: textValue(finding.title, '发现标题', 120),
        summary: textValue(finding.summary, '发现说明', 1_000),
        ...(target ? { target } : {}),
      };
    });
    return {
      type: 'validation', title: textValue(block.title, '校验标题', 100),
      scope: scope as AiConstructionValidationBlock['scope'], counts,
      ...(findings.length ? { findings } : {}),
    };
  }
  if (block.type === 'task_plan') {
    if (!Array.isArray(block.tasks) || block.tasks.length < 1 || block.tasks.length > 30) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '任务计划必须包含 1 到 30 个任务');
    }
    const tasks = block.tasks.map((value) => {
      const task = objectValue(value);
      const id = textValue(task.id, '任务 ID', 100);
      if (!BLOCK_ID_PATTERN.test(id)) throw new AiConstructionMessageStoreError('INVALID_BLOCK', '任务 ID 格式不正确');
      const status = String(task.status ?? '');
      if (!TASK_PLAN_STATUSES.has(status)) throw new AiConstructionMessageStoreError('INVALID_BLOCK', '任务状态不正确');
      const reason = optionalText(task.reason, '任务原因', 500);
      return {
        id, title: textValue(task.title, '任务标题', 160),
        status: status as AiConstructionTaskPlanBlock['tasks'][number]['status'],
        required: task.required === true, ...(reason ? { reason } : {}),
      };
    });
    if (new Set(tasks.map((task) => task.id)).size !== tasks.length) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '任务计划中的 ID 不能重复');
    }
    const note = optionalText(block.note, '任务调整说明', 1_000);
    return { type: 'task_plan', title: textValue(block.title, '任务计划标题', 100), ...(note ? { note } : {}), tasks };
  }
  if (block.type === 'change_plan') {
    const status = String(block.status ?? '');
    if (!CHANGE_PLAN_STATUSES.has(status)) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '建模调整方案状态不正确');
    }
    if (!Array.isArray(block.changes) || block.changes.length < 1 || block.changes.length > 30) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '建模调整方案必须包含 1 到 30 项变更');
    }
    const changes = block.changes.map((value) => {
      const change = objectValue(value);
      const id = textValue(change.id, '变更 ID', 64);
      if (!BLOCK_ID_PATTERN.test(id)) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', '变更 ID 格式不正确');
      }
      const targetKind = String(change.targetKind ?? '');
      const action = String(change.action ?? '');
      if (!CHANGE_TARGET_KINDS.has(targetKind) || !CHANGE_ACTIONS.has(action)) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', '建模调整目标或动作不正确');
      }
      return {
        id,
        targetKind: targetKind as AiConstructionChangePlanBlock['changes'][number]['targetKind'],
        targetKey: textValue(change.targetKey, '变更目标', 200),
        action: action as AiConstructionChangePlanBlock['changes'][number]['action'],
        description: textValue(change.description, '变更说明', MAX_CHANGE_PLAN_DESCRIPTION_LENGTH),
      };
    });
    if (new Set(changes.map((change) => change.id)).size !== changes.length) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '变更 ID 不能重复');
    }
    if (block.risks != null && !Array.isArray(block.risks)) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '风险列表格式不正确');
    }
    const risks = (block.risks ?? []).slice(0, 20)
      .map((risk: unknown) => textValue(risk, '风险说明', 500));
    return {
      type: 'change_plan',
      interactionId: textValue(block.interactionId, '交互 ID', 100),
      title: textValue(block.title, '方案标题', 100),
      summary: textValue(block.summary, '方案摘要', 1_000),
      status: status as AiConstructionChangePlanBlock['status'],
      changes,
      ...(risks.length ? { risks } : {}),
    };
  }
  if (block.type === 'next_actions') {
    if (!Array.isArray(block.actions) || block.actions.length < 1 || block.actions.length > 4) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '下一步动作必须包含 1 到 4 个选项');
    }
    const actions = block.actions.map((value) => {
      const action = objectValue(value);
      const id = textValue(action.id, '动作 ID', 64);
      if (!BLOCK_ID_PATTERN.test(id)) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', '动作 ID 格式不正确');
      }
      const style = String(action.style ?? 'secondary');
      if (!ACTION_STYLES.has(style)) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', '动作样式不正确');
      }
      const description = optionalText(action.description, '动作说明', 200);
      // 动作语义由后端声明：未登记的 id 在写入时就失败，不会变成点了没反应的孤儿按钮
      const kind = assistantActionKind(id);
      if (!kind) {
        throw new AiConstructionMessageStoreError(
          'INVALID_BLOCK',
          `动作 ${id} 未在助手动作目录登记`,
        );
      }
      // 导航到具体文件的动作要带路径，否则前端拿不到目标只能空跳
      const rawPath = action.path === undefined || action.path === null
        ? undefined
        : textValue(action.path, '动作目标路径', 200);
      if (rawPath !== undefined
        && (!PACKAGE_PATH_PATTERN.test(rawPath) || rawPath.split('/').includes('..'))) {
        throw new AiConstructionMessageStoreError('INVALID_BLOCK', '动作目标路径不正确');
      }
      return {
        id,
        kind,
        label: textValue(action.label, '动作名称', 50),
        ...(description ? { description } : {}),
        ...(rawPath ? { path: rawPath } : {}),
        style: style as 'primary' | 'secondary' | 'danger',
      };
    });
    if (new Set(actions.map((item) => item.id)).size !== actions.length) {
      throw new AiConstructionMessageStoreError('INVALID_BLOCK', '动作 ID 不能重复');
    }
    const title = optionalText(block.title, '动作区标题', 100);
    return {
      type: 'next_actions',
      ...(title ? { title } : {}),
      actions,
    };
  }
  throw new AiConstructionMessageStoreError('INVALID_BLOCK', `不支持的消息块：${String(block.type ?? '')}`);
}

function rowToMessage(row: any): AiConstructionMessage {
  return {
    id: row.id,
    projectId: row.project_id,
    ...(row.task_id ? { taskId: row.task_id } : {}),
    ...(row.message_key ? { key: row.message_key } : {}),
    seq: Number(row.seq),
    role: row.role,
    blocks: JSON.parse(row.blocks_json) as AiConstructionMessageBlock[],
    createdAt: row.created_at,
  };
}

// 事件里内联完整消息，前端收到即可上屏，无需为每条消息回源拉整个 session。
// 超过上限的消息只发信号，由前端回源读取，避免个别超大消息把事件表撑大。
const INLINE_MESSAGE_MAX_BYTES = 32_768;

// 把消息写入同步为项目事件，前端经 SSE 实时刷新对话（配合 AuthoringState.publish_event）
export function notifyAiConstructionMessageWritten(
  apply: (command: {
    kind: 'publish_event';
    projectId: string;
    type: string;
    payload?: JsonObject;
  }) => unknown,
): (projectId: string, message: AiConstructionMessage, progressOnly: boolean) => void {
  return (projectId, message, progressOnly) => {
    const serialized = JSON.stringify(message);
    const liveMessage = Buffer.byteLength(serialized, 'utf8') <= INLINE_MESSAGE_MAX_BYTES
      ? JSON.parse(serialized) as JsonObject
      : undefined;
    apply({
      kind: 'publish_event',
      projectId,
      type: 'ai_construction.message.written',
      payload: {
        messageId: message.id, seq: message.seq, role: message.role,
        ...(progressOnly ? { progressOnly: true } : {}),
        ...(liveMessage ? { message: liveMessage } : {}),
      },
    });
  };
}

export class AiConstructionMessageStore {
  constructor(
    private readonly db: DatabaseSync = getDb(),
    private readonly onWrite?: (
      projectId: string,
      message: AiConstructionMessage,
      progressOnly: boolean,
    ) => void,
  ) {}

  // 写入提交后通知（用于项目事件流推送）；通知失败不影响消息本身
  private notifyWrite(
    projectId: string,
    message: AiConstructionMessage,
    progressOnly = false,
  ) {
    try {
      this.onWrite?.(projectId, message, progressOnly);
    } catch { /* 通知通道异常不回滚消息 */ }
  }

  findByKey(projectId: string, key: string): AiConstructionMessage | null {
    const row = this.db.prepare(`SELECT * FROM ai_construction_messages
      WHERE project_id = ? AND message_key = ?`).get(projectId, key);
    return row ? rowToMessage(row) : null;
  }

  ensure(
    projectId: string,
    input: AppendAiConstructionMessageInput & { key: string },
  ): AiConstructionMessage {
    const existing = this.findByKey(projectId, input.key);
    return existing ?? this.append(projectId, input);
  }

  upsert(
    projectId: string,
    input: AppendAiConstructionMessageInput & { key: string },
  ): AiConstructionMessage {
    const existing = this.findByKey(projectId, input.key);
    if (!existing) return this.append(projectId, input);
    if (!MESSAGE_ROLES.has(input.role)) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '消息角色不正确');
    }
    if (input.taskId && !this.db.prepare(`SELECT 1 FROM ai_construction_tasks
      WHERE project_id = ? AND id = ?`).get(projectId, input.taskId)) {
      throw new AiConstructionMessageStoreError('TASK_NOT_FOUND', '消息关联的任务不存在');
    }
    if (!Array.isArray(input.blocks) || input.blocks.length < 1 || input.blocks.length > 12) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '一条消息必须包含 1 到 12 个内容块');
    }
    const blocks = input.blocks.map(normalizeBlock);
    this.db.prepare(`UPDATE ai_construction_messages
      SET task_id = ?, role = ?, blocks_json = ?
      WHERE project_id = ? AND message_key = ?`)
      .run(input.taskId ?? null, input.role, JSON.stringify(blocks), projectId, input.key);
    const updated = this.findByKey(projectId, input.key)!;
    this.notifyWrite(projectId, updated, input.progressOnly === true);
    return updated;
  }

  append(projectId: string, input: AppendAiConstructionMessageInput): AiConstructionMessage {
    if (!this.db.prepare('SELECT 1 FROM projects WHERE id = ?').get(projectId)) {
      throw new AiConstructionMessageStoreError('PROJECT_NOT_FOUND', '项目不存在');
    }
    if (!MESSAGE_ROLES.has(input.role)) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '消息角色不正确');
    }
    const key = input.key == null ? undefined : textValue(input.key, '消息 key', 64);
    if (key && !BLOCK_ID_PATTERN.test(key)) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '消息 key 格式不正确');
    }
    if (input.taskId && !this.db.prepare(`SELECT 1 FROM ai_construction_tasks
      WHERE project_id = ? AND id = ?`).get(projectId, input.taskId)) {
      throw new AiConstructionMessageStoreError('TASK_NOT_FOUND', '消息关联的任务不存在');
    }
    if (!Array.isArray(input.blocks) || input.blocks.length < 1 || input.blocks.length > 12) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '一条消息必须包含 1 到 12 个内容块');
    }
    const blocks = input.blocks.map(normalizeBlock);

    const message = this.transaction(() => {
      const current = this.db.prepare(`SELECT COALESCE(MAX(seq), 0) AS seq
        FROM ai_construction_messages WHERE project_id = ?`).get(projectId) as { seq: number };
      const id = `acmsg_${randomUUID().slice(0, 12)}`;
      const seq = Number(current.seq) + 1;
      const createdAt = nowIso();
      this.db.prepare(`INSERT INTO ai_construction_messages
        (id, project_id, task_id, message_key, seq, role, blocks_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
        .run(
          id,
          projectId,
          input.taskId ?? null,
          key ?? null,
          seq,
          input.role,
          JSON.stringify(blocks),
          createdAt,
        );
      return {
        id,
        projectId,
        ...(input.taskId ? { taskId: input.taskId } : {}),
        ...(key ? { key } : {}),
        seq,
        role: input.role,
        blocks,
        createdAt,
      };
    });
    // 提交后再通知，避免通知方在同一连接上嵌套事务
    this.notifyWrite(projectId, message, input.progressOnly === true);
    return message;
  }

  ensureWelcome(
    projectId: string,
    graph: AiConstructionTaskGraphSnapshot,
  ): AiConstructionMessage {
    if (graph.projectId !== projectId) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '欢迎消息任务图不属于当前项目');
    }
    const existing = this.db.prepare(`SELECT * FROM ai_construction_messages
      WHERE project_id = ? AND message_key = 'welcome'`).get(projectId);
    if (existing) return rowToMessage(existing);

    const milestone = graph.milestones.find((item) => item.id === graph.progress.currentMilestoneId);
    const task = graph.tasks.find((item) => item.id === graph.progress.currentTaskId) ?? graph.tasks[0];
    const milestoneTasks = milestone ? graph.tasks.filter((item) => item.milestoneId === milestone.id) : graph.tasks.slice(0, 1);
    const imported = graph.source === 'package_import';
    return this.append(projectId, {
      key: 'welcome',
      role: 'assistant',
      taskId: task?.id,
      blocks: [
        {
          type: 'status',
          title: 'AI 辅助构建已就绪',
          summary: `当前处于「${milestone?.title ?? '待规划'}」，正在准备「${task?.title ?? '下一项建模任务'}」`,
          milestoneId: milestone?.id,
          taskId: task?.id,
          progress: graph.progress.percent,
          eta: {
            minMinutes: graph.progress.eta.minMinutes,
            maxMinutes: graph.progress.eta.maxMinutes,
            confidence: graph.progress.eta.confidence,
          },
        },
        {
          type: 'text',
          text: imported
            ? '我会先检查现有知识包的结构、证据和校验结果，再与您确认需要补充或改进的内容。'
            : '知识包草稿目录已经创建。请先说明建模目标；目标确认后，我会围绕它分析资料并逐步形成流程、业务操作、对象、规则和确定性脚本。',
        },
        { type: 'task_plan', title: '当前建模计划', note: graph.progress.eta.note,
          tasks: milestoneTasks.map((item) => ({ id: item.id, title: item.title, status: item.status,
            required: item.required, ...(item.reason ? { reason: item.reason } : {}) })) },
        ...(imported ? [{
          type: 'next_actions' as const,
          title: '可以从这里开始',
          actions: [
            { id: 'review_package', label: '检查现有知识包', style: 'primary' as const },
            { id: 'view_validation', label: '查看校验情况', style: 'secondary' as const },
          ],
        }] : []),
      ],
    });
  }

  list(projectId: string, afterSeq = 0, limit = 200): AiConstructionMessage[] {
    if (!Number.isSafeInteger(afterSeq) || afterSeq < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '消息读取范围不正确');
    }
    if (!this.db.prepare('SELECT 1 FROM projects WHERE id = ?').get(projectId)) {
      throw new AiConstructionMessageStoreError('PROJECT_NOT_FOUND', '项目不存在');
    }
    return (this.db.prepare(`SELECT * FROM ai_construction_messages
      WHERE project_id = ? AND seq > ? ORDER BY seq LIMIT ?`)
      .all(projectId, afterSeq, limit) as any[]).map(rowToMessage);
  }

  page(
    projectId: string,
    options: { beforeSeq?: number; limit?: number } = {},
  ): AiConstructionMessagePage {
    const { beforeSeq, limit = 200 } = options;
    if ((beforeSeq !== undefined && (!Number.isSafeInteger(beforeSeq) || beforeSeq < 1))
      || !Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
      throw new AiConstructionMessageStoreError('INVALID_MESSAGE', '消息读取范围不正确');
    }
    if (!this.db.prepare('SELECT 1 FROM projects WHERE id = ?').get(projectId)) {
      throw new AiConstructionMessageStoreError('PROJECT_NOT_FOUND', '项目不存在');
    }
    const rows = (beforeSeq === undefined
      ? this.db.prepare(`SELECT * FROM ai_construction_messages
          WHERE project_id = ? ORDER BY seq DESC LIMIT ?`)
        .all(projectId, limit + 1)
      : this.db.prepare(`SELECT * FROM ai_construction_messages
          WHERE project_id = ? AND seq < ? ORDER BY seq DESC LIMIT ?`)
        .all(projectId, beforeSeq, limit + 1)) as any[];
    const messages = rows.slice(0, limit).reverse().map(rowToMessage);
    return {
      messages,
      hasMore: rows.length > limit,
      oldestSeq: messages[0]?.seq ?? null,
      newestSeq: messages.at(-1)?.seq ?? null,
    };
  }

  private transaction<T>(work: () => T): T {
    const savepoint = `ai_messages_${randomUUID().replaceAll('-', '')}`;
    this.db.exec(`SAVEPOINT ${savepoint}`);
    try {
      const result = work();
      this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
      return result;
    } catch (error) {
      this.db.exec(`ROLLBACK TO SAVEPOINT ${savepoint}`);
      this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
      throw error;
    }
  }
}
