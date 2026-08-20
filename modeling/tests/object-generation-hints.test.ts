import assert from 'node:assert/strict';
import test from 'node:test';
import type { AnalysisResult } from '../src/types.js';
import {
  buildKnowledgeGenerationCandidates,
  buildObjectGenerationHints,
} from '../src/workflow/object-generation-hints.js';

test('领域汇总已确定对象范围时材料候选只补证据不扩张范围', () => {
  const analysis: AnalysisResult = {
    domainOverview: '炼化排产',
    candidateObjects: [{ name: '工艺装置', rationale: '核心对象', sourceTables: ['unit'] }],
    openQuestions: [],
    candidateArtifacts: [{
      kind: 'object', name: '工艺装置负荷', rationale: '近义证据', sources: ['unit'], readiness: 'extractable',
    }, {
      kind: 'object', name: '段落中偶然出现的侧线别名', rationale: '分段线索', sources: ['manual.md'], readiness: 'needs_clarification',
    }],
  };
  const hints = buildObjectGenerationHints(analysis, [{ name: 'unit', rowCount: 0, fields: [] }]);
  assert.equal(hints.length, 1);
  assert.equal(hints[0].name, '工艺装置');
  assert.equal(hints[0].materialEvidence?.length, 1);
});

test('领域汇总没有对象范围时材料候选仍可形成对象提示', () => {
  const analysis: AnalysisResult = {
    domainOverview: '炼化排产', candidateObjects: [], openQuestions: [],
    candidateArtifacts: [{
      kind: 'object', name: '储罐', rationale: '全文证据', sources: ['manual.md'], readiness: 'extractable',
    }],
  };
  assert.deepEqual(buildObjectGenerationHints(analysis, []).map((item) => item.name), ['储罐']);
});

test('表格领域候选存在时首版规则指标不扩张到全部文档线索', () => {
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
    ['装置负荷上限'],
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
