import { createHash } from 'node:crypto';

export type ProductLedgerEvidenceStatus = 'complete' | 'insufficient';
export type ProductLedgerProcessingStatus = 'pending' | 'ready' | 'generated' | 'blocked';

export interface ProductLedgerEntry {
  canonicalItemId: string;
  ruleRefs: string[];
  /** v1 compatibility input; normalized into ruleRefs. */
  ruleId?: string;
  sourceRecordIds: string[];
  sourceLocator: string;
  /** 业务台账字段；旧 v1/v2 快照没有这些字段时保持只读兼容。 */
  name?: string;
  applicableSubjects?: string[];
  triggerScenarios?: string[];
  businessNeeds?: string[];
  structuredGaps?: string[];
  mergeGroupId?: string;
  evidenceStatus: ProductLedgerEvidenceStatus;
  processingStatus: ProductLedgerProcessingStatus;
  insufficiencyReason?: string;
}

export interface ProductLedger {
  schemaVersion: 'product-ledger-v1' | 'product-ledger-v2';
  sourceRevision: string;
  scope: string;
  rawCount: number;
  canonicalCount: number;
  mergedGroupCount: number;
  entries: ProductLedgerEntry[];
  required?: boolean;
  /** Optional source identity for revision-safe v2 snapshots. */
  sourceAnalysisRevision?: string;
  sourceFingerprint?: string;
}

export interface ProductLedgerValidation {
  pass: boolean;
  /**
   * 台账不可据以宣布产品覆盖完整：校验未通过，或条目存在资料不足/阻塞状态。
   * pass 只回答"结构和计数是否自洽"，blocking 才回答"能不能往下走"。
   */
  blocking: boolean;
  errors: string[];
  counts: {
    raw: number;
    canonical: number;
    mergedGroups: number;
    complete: number;
    insufficient: number;
  };
  fingerprint: string;
}

/** 单次台账条目上限；超出说明提交方式错误，不能靠继续追加掩盖。 */
export const MAX_PRODUCT_LEDGER_ENTRIES = 20_000;

export function validateProductLedger(input: unknown): ProductLedgerValidation {
  const errors: string[] = [];
  const ledger = isRecord(input) ? input : {};
  const rawCount = countField(ledger.rawCount, 'rawCount', errors);
  const canonicalCount = countField(ledger.canonicalCount, 'canonicalCount', errors);
  const mergedGroupCount = countField(ledger.mergedGroupCount, 'mergedGroupCount', errors);
  const entries = Array.isArray(ledger.entries) ? ledger.entries : [];

  if (ledger.schemaVersion !== 'product-ledger-v1' && ledger.schemaVersion !== 'product-ledger-v2') errors.push('schemaVersion 必须为 product-ledger-v1 或 product-ledger-v2');
  if (!String(ledger.sourceRevision ?? '').trim()) errors.push('sourceRevision 不能为空');
  if (!String(ledger.scope ?? '').trim()) errors.push('scope 不能为空');
  if (!Array.isArray(ledger.entries)) errors.push('entries 必须是数组');
  // 空台账会让下面所有闭合等式在 0 处成立，从而"真空通过"。
  // 没有条目就是没有事实，必须直接判失败，而不是交给 required 决定。
  if (!entries.length) errors.push('entries 不能为空：没有逐项产品事实的台账不能视为通过');
  if (entries.length > MAX_PRODUCT_LEDGER_ENTRIES) {
    errors.push(`entries 数量 ${entries.length} 超过上限 ${MAX_PRODUCT_LEDGER_ENTRIES}`);
  }
  if (rawCount < canonicalCount) errors.push('rawCount 不能小于 canonicalCount');
  if (entries.length !== canonicalCount) {
    errors.push(`entries 数量 ${entries.length} 与 canonicalCount ${canonicalCount} 不一致`);
  }

  const canonicalIds = new Set<string>();
  const ruleIds = new Set<string>();
  const sourceIds = new Set<string>();
  const mergeGroups = new Set<string>();
  let sourceRecordCount = 0;
  let mergedReduction = 0;
  let complete = 0;
  let insufficient = 0;
  let blockedProcessing = 0;

  entries.forEach((rawEntry, index) => {
    const entry = isRecord(rawEntry) ? rawEntry : {};
    const prefix = `entries[${index}]`;
    const canonicalItemId = text(entry.canonicalItemId);
    const ruleRefs = stringArray(entry.ruleRefs ?? (text(entry.ruleId) ? [entry.ruleId] : []));
    const sourceLocator = text(entry.sourceLocator);
    const sourceRecordIds = stringArray(entry.sourceRecordIds);
    const name = text(entry.name);
    const applicableSubjects = entry.applicableSubjects;
    const triggerScenarios = entry.triggerScenarios;
    const businessNeeds = entry.businessNeeds;
    const evidenceStatus = text(entry.evidenceStatus);
    const processingStatus = text(entry.processingStatus);
    const mergeGroupId = text(entry.mergeGroupId);

    if (!canonicalItemId) errors.push(`${prefix}.canonicalItemId 不能为空`);
    else if (canonicalIds.has(canonicalItemId)) errors.push(`${prefix}.canonicalItemId 重复：${canonicalItemId}`);
    else canonicalIds.add(canonicalItemId);
    if (!ruleRefs.length) errors.push(`${prefix}.ruleRefs 不能为空`);
    for (const ruleId of ruleRefs) ruleIds.add(ruleId);
    if (!sourceLocator) errors.push(`${prefix}.sourceLocator 不能为空`);
    if (!sourceRecordIds.length) errors.push(`${prefix}.sourceRecordIds 不能为空`);
    if (ledger.schemaVersion === 'product-ledger-v2' && !name) {
      errors.push(`${prefix}.name 不能为空`);
    }
    for (const [field, value] of [
      ['applicableSubjects', applicableSubjects],
      ['triggerScenarios', triggerScenarios],
      ['businessNeeds', businessNeeds],
    ] as const) {
      if (value !== undefined && !Array.isArray(value)) errors.push(`${prefix}.${field} 必须是数组`);
    }
    for (const sourceId of sourceRecordIds) {
      if (sourceIds.has(sourceId)) errors.push(`${prefix}.sourceRecordIds 包含重复原始记录：${sourceId}`);
      sourceIds.add(sourceId);
    }
    sourceRecordCount += sourceRecordIds.length;
    mergedReduction += Math.max(0, sourceRecordIds.length - 1);
    if (!['complete', 'insufficient'].includes(evidenceStatus)) {
      errors.push(`${prefix}.evidenceStatus 必须为 complete 或 insufficient`);
    } else if (evidenceStatus === 'complete') complete += 1;
    else {
      insufficient += 1;
      if (!text(entry.insufficiencyReason)) errors.push(`${prefix}.insufficiencyReason 不能为空`);
    }
    if (!['pending', 'ready', 'generated', 'blocked'].includes(processingStatus)) {
      errors.push(`${prefix}.processingStatus 不是受支持的处理状态`);
    } else if (processingStatus === 'blocked') blockedProcessing += 1;
    if (mergeGroupId) mergeGroups.add(mergeGroupId);
    if (sourceRecordIds.length > 1 && !mergeGroupId) {
      errors.push(`${prefix} 多条原始记录归并时必须提供 mergeGroupId`);
    }
  });

  if (sourceRecordCount !== rawCount) {
    errors.push(`sourceRecordIds 展开后共 ${sourceRecordCount} 条，与 rawCount ${rawCount} 不一致`);
  }
  if (mergeGroups.size !== mergedGroupCount) {
    errors.push(`mergeGroupId 共 ${mergeGroups.size} 组，与 mergedGroupCount ${mergedGroupCount} 不一致`);
  }
  if (complete + insufficient !== canonicalCount) {
    errors.push(`资料完整 ${complete} + 不足 ${insufficient} 不等于 canonicalCount ${canonicalCount}`);
  }
  if (rawCount - canonicalCount !== mergedReduction && rawCount >= canonicalCount) {
    errors.push('rawCount - canonicalCount 必须等于各归并项减少的原始记录数之和');
  }

  const fingerprint = `sha256:${createHash('sha256')
    .update(JSON.stringify({ rawCount, canonicalCount, mergedGroupCount, entries }))
    .digest('hex')}`;
  return {
    pass: errors.length === 0,
    blocking: errors.length > 0 || insufficient > 0 || blockedProcessing > 0,
    errors,
    counts: { raw: rawCount, canonical: canonicalCount, mergedGroups: mergedGroupCount, complete, insufficient },
    fingerprint,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function text(value: unknown): string {
  return String(value ?? '').trim();
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(text).filter(Boolean)
    : [];
}

/**
 * 非法计数必须报错。悄悄折算成 0 会让缺失或负数的申报看起来是"零条目台账"，
 * 再配合零处成立的闭合等式变成一次假通过。
 */
function countField(value: unknown, field: string, errors: string[]): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    errors.push(`${field} 必须是非负整数`);
    return Number.isFinite(value as number) ? Math.trunc(value as number) : 0;
  }
  return value;
}
