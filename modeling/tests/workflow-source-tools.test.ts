import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { DataDictTable } from '../src/types.js';
import {
  canonicalizeArtifactAnalysisSources,
  validateArtifactAnalysis,
} from '../src/workflow/engine.js';
import { buildTools } from '../src/workflow/tools.js';
import { DocumentReadSession } from '../src/workflow/document-read-session.js';

function spacedTable(): DataDictTable {
  return {
    name: ' BatchData',
    rowCount: 1,
    fields: [{
      name: 'batch_id',
      inferredType: 'string',
      sampleValues: ['B001'],
      distinctRatio: 1,
    }],
    rows: [['B001']],
  };
}

test('表格工具通过规范化名称访问并返回原始 Sheet 名', async () => {
  const tools = buildTools({ tables: [spacedTable()], documents: [], captured: {} });
  const describe = tools.find((tool) => tool.name === 'describe_table') as any;
  const read = tools.find((tool) => tool.name === 'read_table_rows') as any;

  const described = await describe.execute('call-1', { table: 'BatchData' });
  assert.match(described.content[0].text, /batch_id/);

  const rows = await read.execute('call-2', { table: 'BatchData' });
  const payload = JSON.parse(rows.content[0].text);
  assert.equal(payload.table, ' BatchData');
  assert.equal(payload.rows[0].batch_id, 'B001');
});

test('领域分析提交规范化合法来源并拒绝终止于未知来源', async () => {
  const captured: Record<string, unknown> = {};
  const submit = buildTools({ tables: [spacedTable()], documents: [], captured })
    .find((tool) => tool.name === 'submit_analysis') as any;
  const base = { domainOverview: '领域概览', openQuestions: [] };

  const invalid = await submit.execute('call-1', {
    ...base,
    candidateObjects: [{ name: '未知对象', rationale: '待核实', sourceTables: ['MissingTable'] }],
  });
  assert.equal(invalid.terminate, undefined);
  assert.equal(captured.analysis, undefined);
  assert.match(invalid.content[0].text, /来源表.*不存在/);
  assert.match(invalid.content[0].text, /重新调用 submit_analysis/);

  const valid = await submit.execute('call-2', {
    ...base,
    candidateObjects: [{ name: '批次', rationale: '批次数据', sourceTables: ['BatchData'] }],
  });
  assert.equal(valid.terminate, true);
  assert.deepEqual((captured.analysis as any).candidateObjects[0].sourceTables, [' BatchData']);
});

test('领域分析工具要求提交候选业务目标和候选流程', () => {
  const submit = buildTools({ tables: [spacedTable()], documents: [], captured: {} })
    .find((tool) => tool.name === 'submit_analysis') as any;
  const properties = submit.parameters?.properties ?? {};

  assert.ok(properties.candidateGoals, 'submit_analysis 缺少候选业务目标');
  assert.ok(properties.candidateProcesses, 'submit_analysis 缺少候选业务流程');
});

test('read_document 返回可校验的结构化分页信息并由账本强制连续读取', async () => {
  const documents = [{ filename: 'manual.md', textContent: 'x'.repeat(6_005) }];
  const readSession = new DocumentReadSession(documents, { fullReadMode: true });
  const read = buildTools({ tables: [], documents, captured: {}, documentReadSession: readSession })
    .find((tool) => tool.name === 'read_document') as any;

  const first = await read.execute('call-1', { filename: 'manual.md', offset: 0 });
  const firstPage = JSON.parse(first.content[0].text);
  assert.deepEqual({ ...firstPage, content: undefined }, {
    filename: 'manual.md', offset: 0, endOffset: 6_000, totalChars: 6_005,
    nextOffset: 6_000, eof: false, content: undefined,
  });
  assert.equal(first.details.endOffset, 6_000);

  const skipped = await read.execute('call-2', {
    filename: 'manual.md', offset: 6_004, previousPageSummary: '第一页摘要',
  });
  assert.match(skipped.content[0].text, /expectedOffset.*6000/);

  const last = await read.execute('call-3', {
    filename: 'manual.md', offset: 6_000, previousPageSummary: '第一页摘要',
  });
  assert.equal(JSON.parse(last.content[0].text).eof, true);
  assert.equal(readSession.complete, true);
});

test('材料分类将规范化表名还原为原始 Sheet 名', () => {
  const analysis = {
    materialAssessments: [{
      source: 'BatchData',
      sourceType: 'table' as const,
      suitableFor: ['object' as const],
      rationale: '表格提供批次字段',
    }],
    candidateArtifacts: [{
      kind: 'object' as const,
      name: '加工批次',
      rationale: '表格实体',
      sources: ['BatchData'],
      readiness: 'extractable' as const,
    }],
  };

  const canonical = canonicalizeArtifactAnalysisSources([spacedTable()], [], analysis);
  assert.equal(canonical.materialAssessments?.[0].source, ' BatchData');
  assert.deepEqual(canonical.candidateArtifacts?.[0].sources, [' BatchData']);
  assert.deepEqual(validateArtifactAnalysis([spacedTable()], [], canonical), []);
});
