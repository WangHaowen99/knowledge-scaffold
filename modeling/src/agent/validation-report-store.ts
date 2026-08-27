import { createHash, randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { nowIso } from '../db/index.js';

const INLINE_FINDING_LIMIT = 5;
const INLINE_FINDING_CHAR_LIMIT = 12_000;
const INLINE_REASON_CHAR_LIMIT = 2_000;
const MAX_FINDING_PAGE_SIZE = 50;
const SUMMARY_BUCKET_LIMIT = 20;
const SUMMARY_LABEL_CHAR_LIMIT = 240;

export interface ValidationFailureEnvelope {
  code: string;
  reason: string;
  next: string[];
  details: Record<string, unknown>;
}

interface StoredValidationReportRow {
  id: string;
  tool_name: string;
  error_code: string;
  validator_version: string;
  finding_fingerprint: string;
  finding_count: number;
  summary_json: string;
  created_at: string;
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]),
  );
}

function stableJson(value: unknown): string {
  return JSON.stringify(stableValue(value));
}

function summaryLabel(value: unknown): string {
  const normalized = String(value ?? '').trim() || '(未指定)';
  if (normalized.length <= SUMMARY_LABEL_CHAR_LIMIT) return normalized;
  const digest = createHash('sha256').update(normalized).digest('hex').slice(0, 12);
  return `${normalized.slice(0, SUMMARY_LABEL_CHAR_LIMIT - 16)}...#${digest}`;
}

function findingRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : { message: String(value ?? '') };
}

function findingFingerprint(validatorVersion: string, findings: Record<string, unknown>[]): string {
  const canonicalFindings = findings.map(stableJson).sort();
  return `sha256:${createHash('sha256')
    .update(stableJson({ validatorVersion, findings: canonicalFindings }))
    .digest('hex')}`;
}

function countBy(
  findings: Record<string, unknown>[],
  field: 'path' | 'ruleId',
): Array<Record<string, string | number>> {
  const counts = new Map<string, number>();
  for (const finding of findings) {
    const key = summaryLabel(finding[field]);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const label = field === 'path' ? 'path' : 'ruleId';
  return [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, SUMMARY_BUCKET_LIMIT)
    .map(([key, count]) => ({ [label]: key, count }));
}

function parseObject(raw: string): Record<string, unknown> {
  try {
    const value = JSON.parse(raw);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

export class ValidationReportStore {
  constructor(private readonly db: DatabaseSync) {}

  record(
    projectId: string,
    runId: string,
    toolName: string,
    failure: ValidationFailureEnvelope,
  ): ValidationFailureEnvelope {
    const rawFindings = Array.isArray(failure.details.findings) ? failure.details.findings : [];
    const findings = rawFindings.map(findingRecord);
    const validatorVersion = String(failure.details.validatorVersion ?? 'unknown');
    const fingerprint = findingFingerprint(validatorVersion, findings);
    const reportId = `avr_${randomUUID().replaceAll('-', '')}`;
    const summary = {
      findingCount: findings.length,
      distinctPathCount: new Set(findings.map((finding) => String(finding.path ?? '').trim())).size,
      distinctRuleCount: new Set(findings.map((finding) => String(finding.ruleId ?? '').trim())).size,
      paths: countBy(findings, 'path'),
      rules: countBy(findings, 'ruleId'),
    };
    const fullReport = {
      ...failure.details,
      error: failure.code,
      reason: failure.reason,
      next: failure.next,
      findings,
    };
    const shouldCompact = findings.length > INLINE_FINDING_LIMIT
      || stableJson(findings).length > INLINE_FINDING_CHAR_LIMIT
      || failure.reason.length > INLINE_REASON_CHAR_LIMIT;

    this.db.exec('SAVEPOINT agent_validation_report');
    try {
      this.db.prepare(`INSERT INTO agent_validation_reports (
        id, project_id, run_id, tool_name, error_code, validator_version,
        finding_fingerprint, finding_count, summary_json, report_json, created_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(
        reportId,
        projectId,
        runId,
        toolName,
        failure.code,
        validatorVersion,
        fingerprint,
        findings.length,
        JSON.stringify(summary),
        JSON.stringify(fullReport),
        nowIso(),
      );
      const insertFinding = this.db.prepare(`INSERT INTO agent_validation_findings (
        report_id, ordinal, rule_id, path, message, suggestion, finding_json
      ) VALUES (?,?,?,?,?,?,?)`);
      findings.forEach((finding, ordinal) => insertFinding.run(
        reportId,
        ordinal,
        String(finding.ruleId ?? ''),
        String(finding.path ?? ''),
        String(finding.message ?? ''),
        String(finding.suggestion ?? ''),
        JSON.stringify(finding),
      ));
      this.db.exec('RELEASE SAVEPOINT agent_validation_report');
    } catch (error) {
      this.db.exec('ROLLBACK TO SAVEPOINT agent_validation_report');
      this.db.exec('RELEASE SAVEPOINT agent_validation_report');
      throw error;
    }

    const { findings: _fullFindings, issues: _fullIssues, ...compactDetails } = failure.details;
    return {
      ...failure,
      reason: shouldCompact
        ? `确定性校验未通过，共 ${findings.length} 项；完整报告已保存，可按页读取`
        : failure.reason,
      details: {
        ...compactDetails,
        findingCount: findings.length,
        findingFingerprint: fingerprint,
        validationReportId: reportId,
        findingSummary: summary,
        findingsTruncated: shouldCompact,
        ...(shouldCompact ? {} : { findings }),
      },
    };
  }

  summary(projectId: string, runId: string, reportId: string) {
    const row = this.report(projectId, runId, reportId);
    return {
      reportId: row.id,
      tool: row.tool_name,
      error: row.error_code,
      validatorVersion: row.validator_version,
      findingFingerprint: row.finding_fingerprint,
      findingCount: row.finding_count,
      findingSummary: parseObject(row.summary_json),
      createdAt: row.created_at,
      display: { text: `校验报告包含 ${row.finding_count} 项，需要按页修正` },
    };
  }

  findings(projectId: string, runId: string, reportId: string, offset: number, limit: number) {
    const report = this.report(projectId, runId, reportId);
    const parsedOffset = Number.isFinite(offset) ? Math.max(0, Math.trunc(offset)) : 0;
    const boundedOffset = Math.min(report.finding_count, parsedOffset);
    const parsedLimit = Number.isFinite(limit) ? Math.trunc(limit) : 20;
    const boundedLimit = Math.max(1, Math.min(MAX_FINDING_PAGE_SIZE, parsedLimit || 20));
    const rows = this.db.prepare(`SELECT ordinal, finding_json
      FROM agent_validation_findings
      WHERE report_id = ? AND ordinal >= ?
      ORDER BY ordinal LIMIT ?`).all(reportId, boundedOffset, boundedLimit) as unknown as
      Array<{ ordinal: number; finding_json: string }>;
    const items = rows.map((row) => ({ ...parseObject(row.finding_json), ordinal: row.ordinal }));
    const nextOffset = boundedOffset + items.length;
    const shownStart = items.length ? boundedOffset + 1 : boundedOffset;
    return {
      reportId,
      findingFingerprint: report.finding_fingerprint,
      total: report.finding_count,
      offset: boundedOffset,
      limit: boundedLimit,
      findings: items,
      hasMore: nextOffset < report.finding_count,
      ...(nextOffset < report.finding_count ? { nextOffset } : {}),
      display: { text: `已读取校验项 ${shownStart}-${nextOffset} / ${report.finding_count}` },
    };
  }

  private report(projectId: string, runId: string, reportId: string): StoredValidationReportRow {
    const row = this.db.prepare(`SELECT id, tool_name, error_code, validator_version,
      finding_fingerprint, finding_count, summary_json, created_at
      FROM agent_validation_reports
      WHERE id = ? AND project_id = ? AND run_id = ?`).get(reportId, projectId, runId) as
      StoredValidationReportRow | undefined;
    if (!row) throw new Error('VALIDATION_REPORT_NOT_FOUND');
    return row;
  }
}
