import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import {
  findMaterialPurposeReviewProblems,
  MaterialPurposeReviewStore,
} from '../src/documents/material-purpose-review.js';
import { DocumentRevisionRepo } from '../src/documents/repo.js';
import type { DataDictTable, PackageSpec, SpecObject } from '../src/types.js';

const schema = readFileSync(new URL('../src/db/schema.sql', import.meta.url), 'utf8');

function setup() {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  const now = new Date().toISOString();
  db.prepare(`INSERT INTO projects
    (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES ('project-purpose', 'owner', '合成资料用途测试', '', '', 'ingest', ?, ?)`)
    .run(now, now);
  return {
    db,
    documents: new DocumentRevisionRepo(db),
    reviews: new MaterialPurposeReviewStore(db),
  };
}

function tables(version = 1): DataDictTable[] {
  return [{
    name: 'records',
    rowCount: 1,
    fields: [{ name: 'record_id', inferredType: 'string', sampleValues: [`R-${version}`], distinctRatio: 1 }],
    rows: [[`R-${version}`]],
  }, {
    name: 'links',
    rowCount: 1,
    fields: [
      { name: 'from_id', inferredType: 'string', sampleValues: [`R-${version}`], distinctRatio: 1 },
      { name: 'to_id', inferredType: 'string', sampleValues: [`R-${version + 1}`], distinctRatio: 1 },
    ],
    rows: [[`R-${version}`, `R-${version + 1}`]],
  }, {
    name: 'notes',
    rowCount: 1,
    fields: [{ name: 'note', inferredType: 'string', sampleValues: ['合成说明'], distinctRatio: 1 }],
    rows: [['合成说明']],
  }];
}

function packageSpec(objects: SpecObject[] = []): PackageSpec {
  return {
    id: 'purpose-review-package',
    name: '合成用途评审知识包',
    description: '验证逐 Sheet 分类和对象来源映射。',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects,
    operations: [],
  };
}

test('混合 Sheet 必须经过助手完整建议和用户原子确认', () => {
  const { db, documents, reviews } = setup();
  const document = documents.insert({
    projectId: 'project-purpose',
    filename: 'synthetic-workbook.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: tables(),
  }).document;

  const initial = reviews.ensure(document)!;
  assert.equal(initial.status, 'pending');
  assert.equal(initial.suggestedBy, 'system');
  assert.deepEqual(initial.sheetSuggestions.map((item) => item.sheet), ['records', 'links', 'notes']);
  assert.throws(() => reviews.confirm(document, {
    purpose: 'schema',
    sheets: initial.sheetSuggestions,
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  }), /仍在分析/);

  const userPreference = reviews.suggest(document, {
    purpose: 'schema',
    sheets: [],
    suggestedBy: 'user',
  });
  assert.equal(userPreference.suggestedBy, 'user');
  assert.throws(() => reviews.confirm(document, {
    purpose: 'schema',
    sheets: userPreference.sheetSuggestions,
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  }), /仍在分析/);

  assert.throws(() => reviews.suggest(document, {
    purpose: 'schema',
    sheets: [{ sheet: 'records', semanticRole: 'schema', importDisposition: 'object' }],
    suggestedBy: 'agent',
  }), /尚未确认用途/);

  const suggested = reviews.suggest(document, {
    purpose: 'schema',
    sheets: [
      { sheet: 'records', semanticRole: 'schema', importDisposition: 'object', reason: '一行对应一个实体' },
      { sheet: 'links', semanticRole: 'schema', importDisposition: 'relationship', reason: '两列组成端点' },
      { sheet: 'notes', semanticRole: 'evidence', importDisposition: 'reference', reason: '仅提供业务说明' },
    ],
    suggestedBy: 'agent',
  });
  assert.equal(suggested.status, 'pending');
  assert.deepEqual(suggested.sheetSuggestions.map((item) => item.importDisposition), [
    'object', 'relationship', 'reference',
  ]);
  assert.throws(() => reviews.confirm(document, {
    purpose: 'auto' as any,
    sheets: suggested.sheetSuggestions,
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  }), /不能是“由助手判断”/);

  const confirmed = reviews.confirm(document, {
    purpose: 'schema',
    sheets: suggested.sheetSuggestions,
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  });
  assert.equal(confirmed.document.directiveRevision, 1);
  assert.equal(confirmed.document.purpose, 'schema');
  assert.equal(confirmed.review.status, 'confirmed');
  assert.equal(confirmed.review.confirmedBy, 'owner');
  const ignoredAgentChange = reviews.suggest(confirmed.document, {
    purpose: 'evidence',
    sheets: [
      { sheet: 'records', semanticRole: 'evidence', importDisposition: 'reference' },
      { sheet: 'links', semanticRole: 'evidence', importDisposition: 'reference' },
      { sheet: 'notes', semanticRole: 'evidence', importDisposition: 'reference' },
    ],
    suggestedBy: 'agent',
  });
  assert.equal(ignoredAgentChange.status, 'confirmed');
  assert.equal(ignoredAgentChange.confirmedPurpose, 'schema');
  assert.deepEqual(ignoredAgentChange.sheetDecisions, confirmed.review.sheetDecisions);

  const missing = findMaterialPurposeReviewProblems(db, 'project-purpose', packageSpec());
  assert.deepEqual(missing.map((item) => [item.code, item.sheet]), [
    ['SCHEMA_SHEET_OBJECT_MAPPING_MISSING', 'records'],
  ]);

  const mapped = packageSpec([{
    key: 'record',
    title: '合成记录',
    description: '用于验证来源映射。',
    fields: [{ key: 'id', type: 'string', required: true, sourceColumn: 'record_id' }],
    sourceRefs: [{
      kind: 'table',
      documentId: document.id,
      table: 'records',
      columns: ['record_id'],
    }],
    primaryKey: ['id'],
  }]);
  assert.deepEqual(findMaterialPurposeReviewProblems(db, 'project-purpose', mapped), []);
});

test('批量接受文件用途建议时任一版本冲突都会整批回滚', () => {
  const { db, documents, reviews } = setup();
  const first = documents.insert({
    projectId: 'project-purpose',
    filename: 'synthetic-goal.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 合成目标',
  }).document;
  const second = documents.insert({
    projectId: 'project-purpose',
    filename: 'synthetic-evidence.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 合成依据',
  }).document;
  for (const document of [first, second]) {
    reviews.ensure(document);
    reviews.suggest(document, {
      purpose: document.id === first.id ? 'goal' : 'evidence',
      sheets: [],
      suggestedBy: 'agent',
    });
  }

  const byId = new Map([[first.id, first], [second.id, second]]);
  const ordered = reviews.list('project-purpose').map((review) => byId.get(review.documentId)!);
  assert.throws(() => reviews.confirmSuggestedBatch(ordered.map((document, index) => ({
    document,
    confirmedBy: 'owner',
    expectedDirectiveRevision: index === ordered.length - 1 ? 99 : 0,
  }))), /其他位置更新/);
  assert.deepEqual(reviews.list('project-purpose').map((review) => review.status), ['pending', 'pending']);
  assert.deepEqual((db.prepare(`SELECT directive_revision FROM documents
    WHERE project_id = ? ORDER BY id`).all('project-purpose') as any[])
    .map((row) => Number(row.directive_revision)), [0, 0]);

  const confirmed = reviews.confirmSuggestedBatch([first, second].map((document) => ({
    document,
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  })));
  assert.equal(confirmed.length, 2);
  assert.equal(reviews.progress('project-purpose').allConfirmed, true);
  assert.deepEqual(new Set(confirmed.map((item) => item.document.purpose)), new Set(['goal', 'evidence']));
});

test('上传新修订不会继承旧修订的 Sheet 确认', () => {
  const { documents, reviews } = setup();
  const first = documents.insert({
    projectId: 'project-purpose',
    filename: 'synthetic-workbook.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: tables(),
  }).document;
  reviews.ensure(first);
  const suggestion = reviews.suggest(first, {
    purpose: 'schema',
    sheets: [
      { sheet: 'records', semanticRole: 'schema', importDisposition: 'object' },
      { sheet: 'links', semanticRole: 'schema', importDisposition: 'relationship' },
      { sheet: 'notes', semanticRole: 'ignore', importDisposition: 'ignore' },
    ],
    suggestedBy: 'agent',
  });
  reviews.confirm(first, {
    purpose: 'schema',
    sheets: suggestion.sheetSuggestions,
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  });

  const replacement = documents.insert({
    projectId: 'project-purpose',
    filename: 'synthetic-workbook.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: tables(2),
  }).document;
  const nextReview = reviews.ensure(replacement)!;

  assert.notEqual(replacement.id, first.id);
  assert.equal(nextReview.status, 'pending');
  assert.equal(nextReview.suggestedBy, 'system');
  assert.deepEqual(reviews.list('project-purpose').map((item) => item.documentId), [replacement.id]);
});

test('普通文档先由 Agent 预判文件用途，再由用户确认', () => {
  const { db, documents, reviews } = setup();
  const document = documents.insert({
    projectId: 'project-purpose',
    filename: '业务目标说明.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 建模目标\n\n用于设备故障处置。',
  }).document;

  const initial = reviews.ensure(document)!;
  assert.equal(initial.status, 'pending');
  assert.equal(initial.suggestedBy, 'system');
  assert.deepEqual(initial.sheetSuggestions, []);
  assert.deepEqual(reviews.progress('project-purpose'), {
    totalCount: 1,
    confirmedCount: 0,
    pendingCount: 1,
    allConfirmed: false,
  });
  assert.throws(() => reviews.confirm(document, {
    purpose: 'goal',
    sheets: [],
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  }), /仍在分析/);

  const suggested = reviews.suggest(document, {
    purpose: 'goal',
    sheets: [],
    suggestedBy: 'agent',
  });
  assert.equal(suggested.suggestedPurpose, 'goal');
  assert.throws(() => reviews.suggest(document, {
    purpose: 'schema',
    sheets: [],
    suggestedBy: 'agent',
  }), /数据 Schema/);
  assert.throws(() => reviews.suggest(document, {
    purpose: 'term',
    sheets: [],
    suggestedBy: 'agent',
  }), /包含数据表/);

  assert.deepEqual(findMaterialPurposeReviewProblems(db, 'project-purpose', packageSpec())
    .map((item) => item.code), ['MATERIAL_PURPOSE_REVIEW_REQUIRED']);
  const confirmed = reviews.confirm(document, {
    purpose: 'goal',
    sheets: [],
    confirmedBy: 'owner',
    expectedDirectiveRevision: 0,
  });
  assert.equal(confirmed.document.purpose, 'goal');
  assert.equal(confirmed.review.status, 'confirmed');
  assert.deepEqual(reviews.progress('project-purpose'), {
    totalCount: 1,
    confirmedCount: 1,
    pendingCount: 0,
    allConfirmed: true,
  });
  assert.deepEqual(findMaterialPurposeReviewProblems(db, 'project-purpose', packageSpec()), []);
});

test('空工作簿不会因预设 Schema 导致用途审查创建失败', () => {
  const { documents, reviews } = setup();
  const document = documents.insert({
    projectId: 'project-purpose',
    filename: '空模板.xlsx',
    kind: 'excel',
    purpose: 'schema',
    parseStatus: 'parsed',
    tables: [],
  }).document;

  const review = reviews.ensure(document)!;
  assert.equal(review.status, 'pending');
  assert.equal(review.suggestedPurpose, 'evidence');
  assert.deepEqual(review.sheetSuggestions, []);
});
