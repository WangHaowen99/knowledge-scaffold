// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  collectOntologyEvidence,
  collectTurnEvidence,
  parseOntologyContextOutput,
  parseOntologyEvidenceOutput,
  parseRuntimeEvidenceOutput,
} from './ontologyEvidenceDisplay.js';

const response = {
  contract: 'OntologyEvidenceSearch/v1',
  success: true,
  status: 'resolved',
  result_code: 'EVIDENCE_FOUND',
  ontology_id: 'eco',
  ontology_version: '1.0.0',
  ontology_epoch: 'epoch-1',
  index_generation: 'generation-1',
  query: 'ECO 输入参数',
  retrieval: { mode: 'lexical_graph' },
  hits: [
    {
      evidence_id: 'ont_ev_one',
      resource: 'operation',
      key: 'ECO变更影响分析计算',
      title: 'ECO变更影响分析计算',
      source_path: 'operations/ECO变更影响分析计算.md',
      relevance_score: 0.82,
    },
    {
      evidence_id: 'ont_ev_two',
      resource: 'object',
      key: 'ECO受影响对象',
      title: 'ECO受影响对象',
      source_path: 'objects/ECO受影响对象.md',
      relevance_score: 0.66,
    },
  ],
};

test('parses the complete ontology evidence search contract', () => {
  const parsed = parseOntologyEvidenceOutput(JSON.stringify(response));
  assert.equal(parsed.preview_truncated, false);
  assert.equal(parsed.hits.length, 2);
  assert.equal(parsed.query, 'ECO 输入参数');
});

test('recovers complete hits from a truncated historical preview', () => {
  const full = JSON.stringify(response);
  const truncated = `${full.slice(0, full.indexOf('},{') + 1)},`;
  const parsed = parseOntologyEvidenceOutput(truncated);
  assert.equal(parsed.preview_truncated, true);
  assert.equal(parsed.hits.length, 1);
  assert.equal(parsed.hits[0].evidence_id, 'ont_ev_one');
});

test('collects, ranks, deduplicates and marks evidence loaded by ontology_context', () => {
  const segments = [
    {
      type: 'tool',
      meta: {
        tool_name: 'ontology_evidence_search',
        output_preview: JSON.stringify(response),
      },
    },
    {
      type: 'tool',
      meta: {
        tool_name: 'ontology_context',
        input: { resource: 'operation', key: 'ECO变更影响分析计算' },
      },
    },
  ];
  const result = collectOntologyEvidence(segments);
  assert.equal(result.hits.length, 2);
  assert.equal(result.hits[0].evidence_id, 'ont_ev_one');
  assert.equal(result.hits[0].loaded, true);
  assert.equal(result.hits[1].loaded, false);
  assert.equal(result.retrievalMode, 'lexical_graph');
});

test('projects static source evidence directly from structured ontology context', () => {
  const contextOutput = {
    contract: 'OntologyContext/v2',
    success: true,
    ontology_id: 'eco',
    resource: 'object',
    key: 'ECO变更单',
    section: null,
    content: 'ECO 变更单对象定义。',
    context_meta: {
      ontology_version: '1.0.0',
      ontology_epoch: 'epoch-1',
      index_generation: 'generation-1',
      source_path: 'objects/ECO变更单.md',
      source_evidence_ids: ['ont_ev_context_one', 'ont_ev_context_two'],
    },
  };
  assert.equal(parseOntologyContextOutput(JSON.stringify(contextOutput)).key, 'ECO变更单');

  const result = collectTurnEvidence([{
    type: 'tool',
    meta: {
      tool_name: 'ontology_context',
      input: { resource: 'object', key: 'ECO变更单' },
      output_preview: JSON.stringify(contextOutput),
    },
  }]);

  assert.equal(result.ontology.length, 2);
  assert.equal(result.runtime.length, 0);
  assert.equal(result.ontology[0].title, 'ECO变更单');
  assert.equal(result.ontology[0].loaded, true);
  assert.equal(result.ontologyEpoch, 'epoch-1');
  assert.equal(result.retrievalMode, 'direct_context');
});

test('projects Runtime Fact receipt from business data search output', () => {
  const output = JSON.stringify({
    success: true,
    message: '业务数据查询成功。',
    runtime_evidence: {
      evidence_id: 'ev_69067acabf6749079898fd9ade68ea6f',
      persistence_status: 'persisted',
      grounding_status: 'unverified',
      grounding_warnings: ['selector unresolved'],
      derived_from: [],
    },
  });
  assert.equal(parseRuntimeEvidenceOutput(output).receipt.grounding_status, 'unverified');

  const result = collectTurnEvidence([{
    type: 'tool',
    meta: {
      tool_name: 'business_data_search',
      input: { entity: 'ECO变更单', query: '查询 ECO-001 详细信息' },
      output_preview: output,
    },
  }]);

  assert.equal(result.ontology.length, 0);
  assert.equal(result.runtime.length, 1);
  assert.equal(result.runtime[0].title, 'ECO变更单查询');
  assert.equal(result.runtime[0].evidence_id, 'ev_69067acabf6749079898fd9ade68ea6f');
  assert.equal(result.runtime[0].grounding_status, 'unverified');
});

test('projects ontology lineage from a grounded Runtime Fact even when the model skipped search', () => {
  const output = JSON.stringify({
    success: true,
    runtime_evidence: {
      evidence_id: 'ev_grounded_eco001',
      persistence_status: 'persisted',
      grounding_status: 'grounded',
      grounding_warnings: [],
      derived_from: ['ont_ev_eco_object_one', 'ont_ev_eco_object_two'],
    },
  });
  const result = collectTurnEvidence([{
    type: 'tool',
    meta: {
      tool_name: 'business_data_search',
      input: { entity: 'ECO变更单', query: '查询 ECO-001 详细信息' },
      output_preview: output,
    },
  }]);

  assert.equal(result.ontology.length, 2);
  assert.equal(result.ontology[0].origin, 'runtime_grounding');
  assert.equal(result.ontology[0].title, 'ECO变更单');
  assert.equal(result.runtime.length, 1);
  assert.equal(result.runtime[0].grounding_status, 'grounded');
});

test('combines explicit search hits, loaded context and runtime facts without duplicates', () => {
  const contextOutput = {
    contract: 'OntologyContext/v2',
    success: true,
    ontology_id: 'eco',
    resource: 'operation',
    key: 'ECO变更影响分析计算',
    content: '完整流程定义。',
    context_meta: {
      ontology_version: '1.0.0',
      ontology_epoch: 'epoch-1',
      index_generation: 'generation-1',
      source_path: 'operations/ECO变更影响分析计算.md',
      source_evidence_ids: ['ont_ev_one'],
    },
  };
  const runtimeOutput = JSON.stringify({
    runtime_evidence: {
      evidence_id: 'ev_69067acabf6749079898fd9ade68ea6f',
      persistence_status: 'persisted',
      grounding_status: 'grounded',
      derived_from: ['ont_ev_one'],
    },
  });
  const result = collectTurnEvidence([
    { type: 'tool', meta: { tool_name: 'ontology_evidence_search', output_preview: JSON.stringify(response) } },
    {
      type: 'tool',
      meta: {
        tool_name: 'ontology_context',
        input: { resource: 'operation', key: 'ECO变更影响分析计算' },
        output_preview: JSON.stringify(contextOutput),
      },
    },
    {
      type: 'tool',
      meta: {
        tool_name: 'business_data_search',
        input: { entity: 'ECO受影响对象', query: '查询 ECO-001' },
        output_preview: runtimeOutput,
      },
    },
  ]);

  assert.equal(result.ontology.length, 2);
  assert.equal(result.ontology[0].evidence_id, 'ont_ev_one');
  assert.equal(result.ontology[0].loaded, true);
  assert.equal(result.runtime.length, 1);
  assert.deepEqual(result.runtime[0].derived_from, ['ont_ev_one']);
});

test('prefers bounded evidence projections when public output previews are truncated', () => {
  const contextProjection = {
    contract: 'OntologyContext/v2',
    success: true,
    ontology_id: 'eco',
    resource: 'object',
    key: 'ECO变更单',
    content: 'ECO 变更单对象定义。',
    context_meta: {
      ontology_epoch: 'epoch-projected',
      source_path: 'objects/ECO变更单.md',
      source_evidence_ids: ['ont_ev_projected'],
    },
  };
  const runtimeProjection = {
    contract: 'BusinessDataEvidenceProjection/v1',
    runtime_evidence: {
      evidence_id: 'ev_projected',
      persistence_status: 'persisted',
      grounding_status: 'grounded',
      derived_from: ['ont_ev_projected'],
    },
  };
  const result = collectTurnEvidence([
    {
      key: 'context-block',
      kind: 'tool',
      toolName: 'ontology_context',
      input: { resource: 'object', key: 'ECO变更单' },
      output: '{"contract":"OntologyContext/v2","content":"truncated',
      outputProjection: contextProjection,
      outputTruncated: true,
    },
    {
      key: 'business-block',
      kind: 'business_data',
      toolName: 'business_data_search',
      input: { entity: 'ECO变更单', query: '查询 ECO-001' },
      output: '{"rows":[',
      outputProjection: runtimeProjection,
      outputTruncated: true,
    },
  ]);

  assert.equal(result.ontology.length, 1);
  assert.equal(result.ontology[0].evidence_id, 'ont_ev_projected');
  assert.equal(result.runtime.length, 1);
  assert.equal(result.runtime[0].evidence_id, 'ev_projected');
  assert.equal(result.ontologyEpoch, 'epoch-projected');
});
