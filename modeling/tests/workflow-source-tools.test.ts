import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { DataDictTable } from '../src/types.js';
import {
  canonicalizeArtifactAnalysisSources,
  validateArtifactAnalysis,
} from '../src/workflow/engine.js';
import { buildTools } from '../src/workflow/tools.js';

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

test('read_document 为局部核对返回结构化分页信息', async () => {
  const documents = [{ filename: 'manual.md', textContent: 'x'.repeat(6_005) }];
  const read = buildTools({ tables: [], documents, captured: {} })
    .find((tool) => tool.name === 'read_document') as any;

  const first = await read.execute('call-1', { filename: 'manual.md', offset: 0 });
  const firstPage = JSON.parse(first.content[0].text);
  assert.deepEqual({ ...firstPage, content: undefined }, {
    filename: 'manual.md', offset: 0, endOffset: 6_000, totalChars: 6_005,
    nextOffset: 6_000, eof: false, content: undefined,
  });
  assert.equal(first.details.endOffset, 6_000);

  const last = await read.execute('call-2', { filename: 'manual.md', offset: 6_000 });
  assert.equal(JSON.parse(last.content[0].text).eof, true);
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
