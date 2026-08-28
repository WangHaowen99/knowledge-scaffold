import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import type {
  DocumentRecord,
  MaterialPurpose,
  MaterialPurposeReview,
  MaterialSheetImportDisposition,
  MaterialSheetPurposeDecision,
  MaterialSheetSemanticRole,
  PackageSpec,
} from '../types.js';
import { tableSourcesFor } from '../workflow/source-references.js';

const PURPOSES = new Set<MaterialPurpose>(['auto', 'goal', 'schema', 'evidence', 'term']);
const ROLES = new Set<MaterialSheetSemanticRole>(['schema', 'evidence', 'ignore']);
const DISPOSITIONS = new Set<MaterialSheetImportDisposition>([
  'object', 'relationship', 'derived', 'reference', 'ignore',
]);

export interface MaterialPurposeReviewProblem {
  code: 'MATERIAL_PURPOSE_REVIEW_REQUIRED'
    | 'SCHEMA_SHEET_OBJECT_MAPPING_MISSING'
    | 'SCHEMA_SHEET_OBJECT_MAPPING_DUPLICATED'
    | 'SCHEMA_SHEET_DISPOSITION_MISMATCH';
  documentId: string;
  filename: string;
  sheet?: string;
  message: string;
}

export interface MaterialPurposeReviewProgress {
  totalCount: number;
  confirmedCount: number;
  pendingCount: number;
  allConfirmed: boolean;
}

export class MaterialPurposeReviewError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MaterialPurposeReviewError';
  }
}

function isReviewable(document: DocumentRecord): boolean {
  return document.parseStatus === 'parsed';
}

function defaultPurpose(document: DocumentRecord): MaterialPurpose {
  if (document.purpose && document.purpose !== 'auto') {
    if (['schema', 'term'].includes(document.purpose) && !(document.tables ?? []).length) {
      return 'evidence';
    }
    return document.purpose;
  }
  return (document.tables ?? []).some((table) => table.fields.length > 1 || table.rowCount > 0)
    ? 'schema'
    : 'evidence';
}

function decisionForPurpose(sheet: string, purpose: MaterialPurpose): MaterialSheetPurposeDecision {
  if (purpose === 'schema') {
    return { sheet, semanticRole: 'schema', importDisposition: 'object' };
  }
  return { sheet, semanticRole: 'evidence', importDisposition: 'reference' };
}

function normalizeReason(value: unknown): string | undefined {
  const reason = String(value ?? '').normalize('NFKC').trim();
  return reason ? reason.slice(0, 500) : undefined;
}

function normalizeDecision(raw: unknown, field: string): MaterialSheetPurposeDecision {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new MaterialPurposeReviewError(`${field} 必须是对象`);
  }
  const value = raw as Record<string, unknown>;
  const sheet = String(value.sheet ?? '').normalize('NFKC').trim();
  const semanticRole = String(value.semanticRole ?? value.semantic_role ?? '') as MaterialSheetSemanticRole;
  const importDisposition = String(
    value.importDisposition ?? value.import_disposition ?? '',
  ) as MaterialSheetImportDisposition;
  if (!sheet) throw new MaterialPurposeReviewError(`${field}.sheet 不能为空`);
  if (!ROLES.has(semanticRole)) throw new MaterialPurposeReviewError(`${field}.semanticRole 不正确`);
  if (!DISPOSITIONS.has(importDisposition)) {
    throw new MaterialPurposeReviewError(`${field}.importDisposition 不正确`);
  }
  const expectedRole = importDisposition === 'reference'
    ? 'evidence'
    : importDisposition === 'ignore'
      ? 'ignore'
      : 'schema';
  if (semanticRole !== expectedRole) {
    throw new MaterialPurposeReviewError(`${field} 的语义用途与导入处置不一致`);
  }
  const reason = normalizeReason(value.reason);
  return {
    sheet,
    semanticRole,
    importDisposition,
    ...(reason ? { reason } : {}),
  };
}

function assertPurposeMatchesDecisions(
  purpose: MaterialPurpose,
  decisions: readonly MaterialSheetPurposeDecision[],
): void {
  if (purpose === 'term' && !decisions.length) {
    throw new MaterialPurposeReviewError('只有包含数据表的资料可以作为术语表');
  }
  const hasSchemaSheet = decisions.some((decision) => decision.semanticRole === 'schema');
  if (hasSchemaSheet && purpose !== 'schema') {
    throw new MaterialPurposeReviewError('存在用于实例化或派生的 Sheet 时，资料用途必须是数据 Schema');
  }
  if (purpose === 'schema' && !hasSchemaSheet) {
    throw new MaterialPurposeReviewError('数据 Schema 至少要包含一张实体、关系或汇总 Sheet');
  }
}

function normalizeSheetDecisions(
  document: DocumentRecord,
  values: readonly unknown[],
  purpose: MaterialPurpose,
  options: { fillMissing: boolean },
): MaterialSheetPurposeDecision[] {
  const tables = document.tables ?? [];
  const tableNames = new Set(tables.map((table) => table.name));
  const decisions = values.map((value, index) => normalizeDecision(value, `sheets[${index}]`));
  const seen = new Set<string>();
  for (const decision of decisions) {
    if (!tableNames.has(decision.sheet)) {
      throw new MaterialPurposeReviewError(`资料中不存在 Sheet「${decision.sheet}」`);
    }
    if (seen.has(decision.sheet)) throw new MaterialPurposeReviewError(`Sheet「${decision.sheet}」重复`);
    seen.add(decision.sheet);
  }
  if (options.fillMissing) {
    for (const table of tables) {
      if (!seen.has(table.name)) decisions.push(decisionForPurpose(table.name, purpose));
    }
  } else {
    const missing = tables.filter((table) => !seen.has(table.name)).map((table) => table.name);
    if (missing.length) throw new MaterialPurposeReviewError(`以下 Sheet 尚未确认用途：${missing.join('、')}`);
  }
  return tables.map((table) => decisions.find((decision) => decision.sheet === table.name)!);
}

function parseDecisions(value: string): MaterialSheetPurposeDecision[] {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function rowToReview(row: any): MaterialPurposeReview {
  return {
    documentId: row.document_id,
    projectId: row.project_id,
    contentHash: row.content_hash,
    status: row.status,
    suggestedPurpose: row.suggested_purpose,
    sheetSuggestions: parseDecisions(row.sheet_suggestions_json),
    suggestedBy: row.suggested_by,
    ...(row.confirmed_purpose ? { confirmedPurpose: row.confirmed_purpose } : {}),
    sheetDecisions: parseDecisions(row.sheet_decisions_json),
    ...(row.confirmed_by ? { confirmedBy: row.confirmed_by } : {}),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    ...(row.confirmed_at ? { confirmedAt: row.confirmed_at } : {}),
  };
}

export class MaterialPurposeReviewStore {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  get(documentId: string): MaterialPurposeReview | null {
    const row = this.db.prepare('SELECT * FROM material_purpose_reviews WHERE document_id = ?')
      .get(documentId);
    return row ? rowToReview(row) : null;
  }

  list(projectId: string): MaterialPurposeReview[] {
    return (this.db.prepare(`SELECT reviews.* FROM material_purpose_reviews reviews
      JOIN documents ON documents.id = reviews.document_id
      WHERE reviews.project_id = ? AND documents.lifecycle_status = 'active'
      ORDER BY reviews.created_at, reviews.document_id`).all(projectId) as any[]).map(rowToReview);
  }

  progress(projectId: string): MaterialPurposeReviewProgress {
    const reviews = this.list(projectId);
    const confirmedCount = reviews.filter((review) => review.status === 'confirmed').length;
    const pendingCount = reviews.length - confirmedCount;
    return {
      totalCount: reviews.length,
      confirmedCount,
      pendingCount,
      allConfirmed: reviews.length > 0 && pendingCount === 0,
    };
  }

  ensure(document: DocumentRecord): MaterialPurposeReview | null {
    if (!isReviewable(document)) return null;
    const existing = this.get(document.id);
    if (existing && existing.contentHash === (document.contentHash ?? '')) return existing;
    return this.suggest(document, {
      purpose: defaultPurpose(document),
      sheets: [],
      suggestedBy: 'system',
    });
  }

  suggest(document: DocumentRecord, input: {
    purpose: MaterialPurpose;
    sheets?: readonly unknown[];
    suggestedBy: MaterialPurposeReview['suggestedBy'];
  }): MaterialPurposeReview {
    if (!isReviewable(document)) throw new MaterialPurposeReviewError('只有已解析成功的资料可以审查用途');
    if (!PURPOSES.has(input.purpose)) throw new MaterialPurposeReviewError('资料用途建议不正确');
    const purpose = input.purpose === 'auto' ? defaultPurpose(document) : input.purpose;
    const current = this.get(document.id);
    if (current?.status === 'confirmed'
      && current.contentHash === (document.contentHash ?? '')
      && input.suggestedBy !== 'user') {
      return current;
    }
    const suggestions = normalizeSheetDecisions(document, input.sheets ?? [], purpose, {
      fillMissing: input.suggestedBy !== 'agent',
    });
    assertPurposeMatchesDecisions(purpose, suggestions);
    const alreadyConfirmed = current?.status === 'confirmed'
      && current.confirmedPurpose === purpose
      && JSON.stringify(current.sheetDecisions) === JSON.stringify(suggestions);
    const now = nowIso();
    this.db.prepare(`INSERT INTO material_purpose_reviews (
      document_id, project_id, content_hash, status, suggested_purpose,
      sheet_suggestions_json, suggested_by, confirmed_purpose, sheet_decisions_json,
      confirmed_by, created_at, updated_at, confirmed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '[]', NULL, ?, ?, NULL)
    ON CONFLICT(document_id) DO UPDATE SET
      content_hash = excluded.content_hash,
      status = CASE WHEN ? THEN material_purpose_reviews.status ELSE 'pending' END,
      suggested_purpose = excluded.suggested_purpose,
      sheet_suggestions_json = excluded.sheet_suggestions_json,
      suggested_by = excluded.suggested_by,
      confirmed_purpose = CASE WHEN ? THEN material_purpose_reviews.confirmed_purpose ELSE NULL END,
      sheet_decisions_json = CASE WHEN ? THEN material_purpose_reviews.sheet_decisions_json ELSE '[]' END,
      confirmed_by = CASE WHEN ? THEN material_purpose_reviews.confirmed_by ELSE NULL END,
      updated_at = excluded.updated_at,
      confirmed_at = CASE WHEN ? THEN material_purpose_reviews.confirmed_at ELSE NULL END`)
      .run(
        document.id, document.projectId, document.contentHash ?? '', alreadyConfirmed ? 'confirmed' : 'pending',
        purpose, JSON.stringify(suggestions), input.suggestedBy, now, now,
        alreadyConfirmed ? 1 : 0,
        alreadyConfirmed ? 1 : 0,
        alreadyConfirmed ? 1 : 0,
        alreadyConfirmed ? 1 : 0,
        alreadyConfirmed ? 1 : 0,
      );
    return this.get(document.id)!;
  }

  confirm(document: DocumentRecord, input: {
    purpose: Exclude<MaterialPurpose, 'auto'>;
    sheets: readonly unknown[];
    confirmedBy: string;
    expectedDirectiveRevision: number;
  }): { document: DocumentRecord; review: MaterialPurposeReview } {
    if (!isReviewable(document)) throw new MaterialPurposeReviewError('只有已解析成功的资料可以确认用途');
    if (!PURPOSES.has(input.purpose as MaterialPurpose) || String(input.purpose) === 'auto') {
      throw new MaterialPurposeReviewError('确认后的资料用途不能是“由助手判断”');
    }
    const currentReview = this.get(document.id) ?? this.ensure(document);
    if (!currentReview || (currentReview.status !== 'confirmed' && currentReview.suggestedBy !== 'agent')) {
      throw new MaterialPurposeReviewError('资料用途仍在分析，请等待助手完成逐 Sheet 建议');
    }
    const decisions = normalizeSheetDecisions(document, input.sheets, input.purpose, { fillMissing: false });
    assertPurposeMatchesDecisions(input.purpose, decisions);
    const expected = input.expectedDirectiveRevision;
    if (!Number.isSafeInteger(expected) || expected < 0) {
      throw new MaterialPurposeReviewError('缺少有效的资料设置版本，请刷新后重试');
    }
    const now = nowIso();
    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      const updated = this.db.prepare(`UPDATE documents
        SET purpose = ?, directive_revision = directive_revision + 1
        WHERE id = ? AND lifecycle_status = 'active' AND directive_revision = ?`)
        .run(input.purpose, document.id, expected);
      if (Number(updated.changes) !== 1) {
        throw new MaterialPurposeReviewError('资料设置已在其他位置更新，请刷新后重试');
      }
      const suggestions = currentReview.sheetSuggestions.length
        ? currentReview.sheetSuggestions
        : normalizeSheetDecisions(document, [], input.purpose, { fillMissing: true });
      this.db.prepare(`INSERT INTO material_purpose_reviews (
        document_id, project_id, content_hash, status, suggested_purpose,
        sheet_suggestions_json, suggested_by, confirmed_purpose, sheet_decisions_json,
        confirmed_by, created_at, updated_at, confirmed_at
      ) VALUES (?, ?, ?, 'confirmed', ?, ?, 'user', ?, ?, ?, ?, ?, ?)
      ON CONFLICT(document_id) DO UPDATE SET
        content_hash = excluded.content_hash,
        status = 'confirmed',
        confirmed_purpose = excluded.confirmed_purpose,
        sheet_decisions_json = excluded.sheet_decisions_json,
        confirmed_by = excluded.confirmed_by,
        updated_at = excluded.updated_at,
        confirmed_at = excluded.confirmed_at`)
        .run(
          document.id, document.projectId, document.contentHash ?? '', input.purpose,
          JSON.stringify(suggestions), input.purpose, JSON.stringify(decisions),
          input.confirmedBy, now, now, now,
        );
      if (ownsTransaction) this.db.exec('COMMIT');
    } catch (error) {
      if (ownsTransaction && this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    const row = this.db.prepare('SELECT * FROM documents WHERE id = ?').get(document.id) as any;
    return {
      document: {
        ...document,
        purpose: row.purpose,
        directiveRevision: Number(row.directive_revision),
      },
      review: this.get(document.id)!,
    };
  }

  confirmSuggestedBatch(inputs: readonly {
    document: DocumentRecord;
    confirmedBy: string;
    expectedDirectiveRevision: number;
  }[]): { document: DocumentRecord; review: MaterialPurposeReview }[] {
    if (!inputs.length) throw new MaterialPurposeReviewError('没有可确认的资料用途');
    const projectId = inputs[0].document.projectId;
    const byDocumentId = new Map<string, (typeof inputs)[number]>();
    for (const input of inputs) {
      if (input.document.projectId !== projectId) {
        throw new MaterialPurposeReviewError('批量确认只能包含同一项目的资料');
      }
      if (byDocumentId.has(input.document.id)) {
        throw new MaterialPurposeReviewError('批量确认中存在重复资料');
      }
      byDocumentId.set(input.document.id, input);
    }

    const pending = this.list(projectId).filter((review) => review.status === 'pending');
    if (!pending.length) return [];
    for (const review of pending) {
      if (!byDocumentId.has(review.documentId)) {
        throw new MaterialPurposeReviewError('资料用途列表已更新，请刷新后重试');
      }
      if (review.suggestedBy !== 'agent') {
        throw new MaterialPurposeReviewError('资料用途仍在分析，请等待助手完成建议');
      }
      if (review.suggestedPurpose === 'auto') {
        throw new MaterialPurposeReviewError('助手尚未给出明确的资料用途建议');
      }
    }

    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      const confirmed = pending.map((review) => {
        const input = byDocumentId.get(review.documentId)!;
        return this.confirm(input.document, {
          purpose: review.suggestedPurpose as Exclude<MaterialPurpose, 'auto'>,
          sheets: review.sheetSuggestions,
          confirmedBy: input.confirmedBy,
          expectedDirectiveRevision: input.expectedDirectiveRevision,
        });
      });
      if (ownsTransaction) this.db.exec('COMMIT');
      return confirmed;
    } catch (error) {
      if (ownsTransaction && this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }
}

export function findMaterialPurposeReviewProblems(
  db: DatabaseSync,
  projectId: string,
  spec: PackageSpec,
): MaterialPurposeReviewProblem[] {
  const reviews = new MaterialPurposeReviewStore(db).list(projectId);
  const documents = new Map((db.prepare(`SELECT id, filename FROM documents
    WHERE project_id = ? AND lifecycle_status = 'active'`).all(projectId) as any[])
    .map((row) => [String(row.id), String(row.filename)]));
  const problems: MaterialPurposeReviewProblem[] = [];
  for (const review of reviews) {
    const filename = documents.get(review.documentId) ?? review.documentId;
    if (review.status !== 'confirmed') {
      problems.push({
        code: 'MATERIAL_PURPOSE_REVIEW_REQUIRED',
        documentId: review.documentId,
        filename,
        message: review?.sheetSuggestions.length
          ? `表格资料《${filename}》的文件用途和逐 Sheet 处置尚未由用户确认`
          : `资料《${filename}》的用途尚未由用户确认`,
      });
      continue;
    }
    for (const decision of review.sheetDecisions) {
      const boundObjects = spec.objects.filter((object) => tableSourcesFor(object).some((source) => (
        source.documentId === review.documentId && source.table === decision.sheet
      )));
      if (decision.importDisposition === 'object') {
        if (!boundObjects.length) {
          problems.push({
            code: 'SCHEMA_SHEET_OBJECT_MAPPING_MISSING',
            documentId: review.documentId,
            filename,
            sheet: decision.sheet,
            message: `Sheet「${decision.sheet}」已确认为实体数据，但知识包没有对应的结构化对象来源绑定`,
          });
        } else if (boundObjects.length > 1) {
          problems.push({
            code: 'SCHEMA_SHEET_OBJECT_MAPPING_DUPLICATED',
            documentId: review.documentId,
            filename,
            sheet: decision.sheet,
            message: `Sheet「${decision.sheet}」同时绑定了多个对象：${boundObjects.map((object) => object.key).join('、')}`,
          });
        }
      } else if (boundObjects.length) {
        problems.push({
          code: 'SCHEMA_SHEET_DISPOSITION_MISMATCH',
          documentId: review.documentId,
          filename,
          sheet: decision.sheet,
          message: `Sheet「${decision.sheet}」已确认为${decision.importDisposition}，但仍被对象 ${boundObjects.map((object) => object.key).join('、')} 作为实体数据绑定`,
        });
      }
    }
  }
  return problems;
}
