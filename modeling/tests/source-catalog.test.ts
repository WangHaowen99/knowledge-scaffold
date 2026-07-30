import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { AnalysisResult, DataDictTable, SpecObject } from '../src/types.js';
import { createSourceCatalog } from '../src/workflow/source-catalog.js';

function table(name: string, rowCount: number): DataDictTable {
  return {
    name,
    rowCount,
    fields: [{
      name: '序号',
      inferredType: 'number',
      sampleValues: ['1'],
      distinctRatio: 1,
    }],
  };
}

test('来源目录将自身展示标签还原为真实 Sheet 名', () => {
  const catalog = createSourceCatalog([table('十一个穿透式监管场景', 11)]);

  assert.deepEqual(catalog.resolveTable('十一个穿透式监管场景（11 行）'), {
    status: 'resolved',
    tableName: '十一个穿透式监管场景',
    matchedBy: 'display',
  });
});

test('真实 Sheet 名与展示标签冲突时精确名称优先', () => {
  const catalog = createSourceCatalog([
    table('交易（11 行）', 3),
    table('交易', 11),
  ]);

  assert.deepEqual(catalog.resolveTable('交易（11 行）'), {
    status: 'resolved',
    tableName: '交易（11 行）',
    matchedBy: 'exact',
  });
});

test('来源目录兼容 Sheet 名首尾空白并保留原始名称', () => {
  const catalog = createSourceCatalog([table(' BatchData ', 4)]);

  assert.deepEqual(catalog.resolveTable('BatchData'), {
    status: 'resolved',
    tableName: ' BatchData ',
    matchedBy: 'normalized',
  });
  assert.deepEqual(catalog.resolveTable('BatchData（4 行）'), {
    status: 'resolved',
    tableName: ' BatchData ',
    matchedBy: 'normalized',
  });
});

test('规范化后重名的 Sheet 不做静默猜测', () => {
  const catalog = createSourceCatalog([
    table(' Sheet', 1),
    table('Sheet ', 2),
  ]);

  assert.deepEqual(catalog.resolveTable('Sheet'), {
    status: 'ambiguous',
    reference: 'Sheet',
    candidates: [' Sheet', 'Sheet '],
  });
});

test('领域分析中的展示标签在持久化前规范化为真实 Sheet 名', () => {
  const catalog = createSourceCatalog([table('十一个穿透式监管场景', 11)]);
  const analysis: AnalysisResult = {
    domainOverview: '国企穿透式监管',
    candidateObjects: [{
      name: '监管场景',
      rationale: '监管领域核心对象',
      sourceTables: ['十一个穿透式监管场景（11 行）'],
    }],
    openQuestions: [],
  };

  assert.deepEqual(catalog.canonicalizeAnalysis(analysis), {
    value: {
      ...analysis,
      candidateObjects: [{
        ...analysis.candidateObjects[0],
        sourceTables: ['十一个穿透式监管场景'],
      }],
    },
    problems: [],
  });
  assert.equal(analysis.candidateObjects[0].sourceTables[0], '十一个穿透式监管场景（11 行）');
});

test('生成对象的新旧表格来源统一规范化为真实 Sheet 名', () => {
  const catalog = createSourceCatalog([table('十一个穿透式监管场景', 11)]);
  const object: SpecObject = {
    key: 'regulatory_scenario',
    title: '监管场景',
    description: '监管场景',
    sourceTable: '十一个穿透式监管场景（11 行）',
    sourceRefs: [{
      kind: 'table',
      table: '十一个穿透式监管场景（11 行）',
      columns: ['序号'],
    }],
    fields: [{ key: 'id', cnName: '序号', sourceColumn: '序号' }],
    primaryKey: ['id'],
  };

  assert.deepEqual(catalog.canonicalizeObjects([object]), {
    value: [{
      ...object,
      sourceTable: '十一个穿透式监管场景',
      sourceRefs: [{
        kind: 'table',
        table: '十一个穿透式监管场景',
        columns: ['序号'],
      }],
    }],
    problems: [],
  });
});

test('文档来源按真实资料补齐修订 ID 并规范化文件名', () => {
  const catalog = createSourceCatalog([], [{ id: 'doc_policy', filename: '审批制度.md' }]);
  const byFilename: SpecObject = {
    key: 'approval_rule', title: '审批规则', description: '审批规则', fields: [],
    sourceRefs: [{ kind: 'document', filename: '审批制度.md', anchors: [{ heading: '审批条件' }] }],
  };
  const byRevision: SpecObject = {
    key: 'approval_result', title: '审批结果', description: '审批结果', fields: [],
    sourceRefs: [{ kind: 'document', documentId: 'doc_policy', filename: '旧文件名.md' }],
  };

  assert.deepEqual(catalog.canonicalizeObjects([byFilename, byRevision]), {
    value: [{
      ...byFilename,
      sourceRefs: [{
        kind: 'document', filename: '审批制度.md', documentId: 'doc_policy',
        anchors: [{ heading: '审批条件' }],
      }],
    }, {
      ...byRevision,
      sourceRefs: [{ kind: 'document', documentId: 'doc_policy', filename: '审批制度.md' }],
    }],
    problems: [],
  });
});

test('文档来源不存在或同名修订不唯一时拒绝静默绑定', () => {
  const catalog = createSourceCatalog([], [
    { id: 'doc_first', filename: '制度.md' },
    { id: 'doc_second', filename: '制度.md' },
  ]);
  const objects: SpecObject[] = [{
    key: 'ambiguous', title: '同名来源', description: '同名来源', fields: [],
    sourceRefs: [{ kind: 'document', filename: '制度.md' }],
  }, {
    key: 'missing', title: '缺失来源', description: '缺失来源', fields: [],
    sourceRefs: [{ kind: 'document', documentId: 'doc_missing', filename: '缺失.md' }],
  }];

  const result = catalog.canonicalizeObjects(objects);

  assert.deepEqual(result.value, objects);
  assert.deepEqual(result.problems.map((problem) => problem.reason), ['ambiguous', 'missing']);
  assert.match(result.problems[0].message, /多个修订/);
  assert.match(result.problems[1].message, /资料修订.*不存在/);
});
