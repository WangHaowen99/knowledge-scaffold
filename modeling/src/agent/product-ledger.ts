import { createHash } from 'node:crypto';

export type ProductLedgerEvidenceStatus = 'complete' | 'insufficient';
export type ProductLedgerProcessingStatus = 'pending' | 'ready' | 'generated' | 'blocked';

export interface ProductLedgerEntry {
  canonicalItemId: string;
  ruleId: string;
  sourceRecordIds: string[];
  sourceLocator: string;
  mergeGroupId?: string;
  evidenceStatus: ProductLedgerEvidenceStatus;
  processingStatus: ProductLedgerProcessingStatus;
  insufficiencyReason?: string;
}

export interface ProductLedger {
  schemaVersion: 'product-ledger-v1';
  sourceRevision: string;
  scope: string;
  rawCount: number;
  canonicalCount: number;
  mergedGroupCount: number;
  entries: ProductLedgerEntry[];
  required?: boolean;
}

export interface ProductLedgerValidation {
  pass: boolean;
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

export function validateProductLedger(input: unknown): ProductLedgerValidation {
  const errors: string[] = [];
  const ledger = isRecord(input) ? input : {};
  const rawCount = nonNegativeInteger(ledger.rawCount);
  const canonicalCount = nonNegativeInteger(ledger.canonicalCount);
  const mergedGroupCount = nonNegativeInteger(ledger.mergedGroupCount);
  const entries = Array.isArray(ledger.entries) ? ledger.entries : [];

  if (ledger.schemaVersion !== 'product-ledger-v1') errors.push('schemaVersion 必须为 product-ledger-v1');
  if (!String(ledger.sourceRevision ?? '').trim()) errors.push('sourceRevision 不能为空');
  if (!String(ledger.scope ?? '').trim()) errors.push('scope 不能为空');
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

  entries.forEach((rawEntry, index) => {
    const entry = isRecord(rawEntry) ? rawEntry : {};
    const prefix = `entries[${index}]`;
    const canonicalItemId = text(entry.canonicalItemId);
    const ruleId = text(entry.ruleId);
    const sourceLocator = text(entry.sourceLocator);
    const sourceRecordIds = stringArray(entry.sourceRecordIds);
    const evidenceStatus = text(entry.evidenceStatus);
    const processingStatus = text(entry.processingStatus);
    const mergeGroupId = text(entry.mergeGroupId);

    if (!canonicalItemId) errors.push(`${prefix}.canonicalItemId 不能为空`);
    else if (canonicalIds.has(canonicalItemId)) errors.push(`${prefix}.canonicalItemId 重复：${canonicalItemId}`);
    else canonicalIds.add(canonicalItemId);
    if (!ruleId) errors.push(`${prefix}.ruleId 不能为空`);
    else if (ruleIds.has(ruleId)) errors.push(`${prefix}.ruleId 重复：${ruleId}`);
    else ruleIds.add(ruleId);
    if (!sourceLocator) errors.push(`${prefix}.sourceLocator 不能为空`);
    if (!sourceRecordIds.length) errors.push(`${prefix}.sourceRecordIds 不能为空`);
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
    }
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

function nonNegativeInteger(value: unknown): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}
