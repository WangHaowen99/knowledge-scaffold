import assert from 'node:assert/strict';
import test from 'node:test';
import type { AnalysisResult } from '../src/types.js';
import {
  buildKnowledgeGenerationCandidates,
  buildObjectGenerationHints,
} from '../src/workflow/object-generation-hints.js';
import { validateObjectBatch } from '../src/workflow/spec-validation.js';

test('存在 Excel Sheet 时对象只采用确定性物理对象，不再追加文档或逻辑对象', () => {
  const analysis: AnalysisResult = {
    domainOverview: '炼化排产',
    candidateObjects: [
      { name: '工艺装置', rationale: '物理对象', sourceTables: ['unit'] },
      { name: '装置网络', rationale: '逻辑聚合对象', sourceTables: [] },
    ],
    openQuestions: [],
    candidateArtifacts: [{
      kind: 'object', name: '工艺装置负荷', rationale: '近义证据', sources: ['unit'], readiness: 'extractable',
    }, {
      kind: 'object', name: '段落中偶然出现的侧线别名', rationale: '分段线索', sources: ['manual.md'], readiness: 'needs_clarification',
    }],
  };
  const hints = buildObjectGenerationHints(analysis, [{ name: 'unit', documentId: 'doc-excel', rowCount: 0, fields: [] }]);
  assert.deepEqual(hints, []);
});

test('纯文档的可提取对象候选仍可形成逻辑对象提示', () => {
  const analysis: AnalysisResult = {
    domainOverview: '炼化排产', candidateObjects: [], openQuestions: [],
    candidateArtifacts: [{
      kind: 'object', name: '储罐', rationale: '全文证据', sources: ['manual.md'], readiness: 'extractable',
    }],
  };
  assert.deepEqual(buildObjectGenerationHints(analysis, []).map((item) => item.name), ['储罐']);
});

test('绑定表格的材料对象候选不会与确定性物理对象重复生成', () => {
  const analysis: AnalysisResult = {
    domainOverview: '炼化排产', candidateObjects: [], openQuestions: [],
    candidateArtifacts: [{
      kind: 'object', name: '装置', rationale: '表格对象', sources: ['unit'], readiness: 'extractable',
    }],
  };
  assert.deepEqual(buildObjectGenerationHints(analysis, [{ name: 'unit', documentId: 'doc-excel', rowCount: 1, fields: [] }]), []);
});

test('Excel 只约束对象范围，首版规则指标仍保留全文候选供流程相关性处置', () => {
  const analysis: AnalysisResult = {
    domainOverview: '炼化排产', candidateObjects: [], openQuestions: [],
    candidateArtifacts: [{
      kind: 'rule', name: '装置负荷上限', rationale: '领域规则', sources: ['unit'], readiness: 'extractable',
    }, {
      kind: 'metric', name: '局部温度指标', rationale: '文档线索', sources: ['manual.md'], readiness: 'extractable',
    }],
  };
  assert.deepEqual(
    buildKnowledgeGenerationCandidates(analysis, [{ name: 'unit', rowCount: 0, fields: [] }])
      .map((item) => item.name),
    ['装置负荷上限', '局部温度指标'],
  );
});

test('纯文档项目保留全文分析得到的规则指标候选', () => {
  const analysis: AnalysisResult = {
    domainOverview: '制度', candidateObjects: [], openQuestions: [],
    candidateArtifacts: [{
      kind: 'rule', name: '审批规则', rationale: '文档证据', sources: ['policy.md'], readiness: 'extractable',
    }],
  };
  assert.deepEqual(buildKnowledgeGenerationCandidates(analysis, []).map((item) => item.name), ['审批规则']);
});

test('进入逻辑对象生成计划的候选不得静默省略', () => {
  const candidates = [
    { id: 'candidate_1', name: '装置网络', rationale: '逻辑聚合', sourceTables: [] },
    { id: 'candidate_2', name: '排产批次', rationale: '流程输入', sourceTables: [] },
  ];
  const issues = validateObjectBatch(candidates, [{
    candidateId: 'candidate_1', key: 'network', title: '装置网络', description: '逻辑对象', fields: [],
  }]);
  assert.ok(issues.some((issue) => issue.includes('排产批次') && issue.includes('未生成')));
});
