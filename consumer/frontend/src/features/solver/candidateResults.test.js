import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeCandidateResults } from './candidateResults.js';

const artifact = () => {
  const itemIds = Array.from({ length: 9 }, (_, index) => `batch_${index + 1}`);
  const definitions = [
    [1, 'ALL_1', Array(9).fill('OPTION_1')],
    [2, 'ALL_0', Array(9).fill('OPTION_0')],
  ];
  for (let switchPoint = 1; switchPoint < 9; switchPoint += 1) {
    definitions.push(
      [
        2 * switchPoint + 1,
        `OPTION_1_TO_OPTION_0_K${String(switchPoint).padStart(4, '0')}`,
        [...Array(switchPoint).fill('OPTION_1'), ...Array(9 - switchPoint).fill('OPTION_0')],
      ],
      [
        2 * switchPoint + 2,
        `OPTION_0_TO_OPTION_1_K${String(switchPoint).padStart(4, '0')}`,
        [...Array(switchPoint).fill('OPTION_0'), ...Array(9 - switchPoint).fill('OPTION_1')],
      ],
    );
  }
  const ranks = Array.from({ length: 18 }, (_, index) => index);
  [ranks[5], ranks[9]] = [ranks[9], ranks[5]];
  const candidates = definitions.map(([combinationNo, candidateId, assignment]) => ({
    combination_no: combinationNo,
    candidate_id: candidateId,
    assignment,
    eligible: true,
    validation_status: 'feasible',
    metrics: { candidate_rank: ranks[combinationNo - 1] },
    rejection_reasons: [],
    selected: combinationNo === 10,
  })).reverse();
  return {
    schema_version: 'solver.candidate-results.v1',
    problem_digest: `sha256:${'a'.repeat(64)}`,
    candidate_source_kind: 'problem_ir.candidate_selection',
    candidate_source_digest: `sha256:${'b'.repeat(64)}`,
    presentation_revision: 'ordered_binary_route.presentation.v1',
    item_ids: itemIds,
    option_labels: { OPTION_0: '柴油加氢', OPTION_1: '蜡油加氢' },
    candidate_count: 18,
    selected_candidate_id: 'OPTION_0_TO_OPTION_1_K0004',
    candidates,
  };
};

test('candidate results use business combination order and preserve technical rank', () => {
  const normalized = normalizeCandidateResults(artifact());

  assert.deepEqual(
    normalized.rows.map((row) => row.combinationNo),
    Array.from({ length: 18 }, (_, index) => index + 1),
  );
  const winner = normalized.rows.find((row) => row.selected);
  assert.equal(winner.combinationNo, 10);
  assert.equal(winner.metrics.candidate_rank, 5);
  assert.deepEqual(winner.sequenceLabels, [
    '柴油加氢', '柴油加氢', '柴油加氢', '柴油加氢',
    '蜡油加氢', '蜡油加氢', '蜡油加氢', '蜡油加氢', '蜡油加氢',
  ]);
});

test('candidate results reject incomplete numbering and winner drift', () => {
  const missing = artifact();
  missing.candidates.pop();
  assert.throws(() => normalizeCandidateResults(missing), /candidate count/i);

  const winnerDrift = artifact();
  winnerDrift.candidates.find((row) => row.selected).selected = false;
  assert.throws(() => normalizeCandidateResults(winnerDrift), /selected candidate/i);
});

const described = () => {
  const payload = artifact();
  // Scaled integers, as the solver stores them: the descriptors below divide by
  // 100. Values are synthetic round numbers, only their shape matters here.
  payload.candidates.forEach((candidate, index) => {
    candidate.metrics.primary_score = 900000 - index * 1000;
    candidate.metrics.safety_margin = index === 0 ? -500 : 250;
  });
  payload.metric_display = [
    { key: 'primary_score', label: '利润', unit: '元', scale: 100, sense: 'maximize' },
    { key: 'safety_margin', label: '最小余量', unit: '%', scale: 100, sense: 'maximize' },
  ];
  return payload;
};

test('candidate results carry the scale and unit needed to compare solver metrics', () => {
  const normalized = normalizeCandidateResults(described());

  assert.deepEqual(normalized.metricDisplay.map((descriptor) => descriptor.key), [
    'primary_score', 'safety_margin',
  ]);
  assert.deepEqual(normalized.metricDisplay[0], {
    key: 'primary_score', label: '利润', unit: '元', scale: 100, sense: 'maximize',
  });
});

test('candidate results stay valid when the artifact describes no metrics', () => {
  const normalized = normalizeCandidateResults(artifact());

  assert.deepEqual(normalized.metricDisplay, []);
});

test('candidate results reject metric descriptors that cannot be rendered', () => {
  const badScale = described();
  badScale.metric_display[0].scale = 0;
  assert.throws(() => normalizeCandidateResults(badScale), /metric display scale/i);

  const badSense = described();
  badSense.metric_display[0].sense = 'unknown';
  assert.throws(() => normalizeCandidateResults(badSense), /metric display sense/i);

  const duplicated = described();
  duplicated.metric_display[1].key = 'primary_score';
  assert.throws(() => normalizeCandidateResults(duplicated), /metric display keys/i);

  const absent = described();
  absent.metric_display[0].key = 'absent_metric';
  assert.throws(() => normalizeCandidateResults(absent), /metric display value/i);

  const unknownField = described();
  unknownField.metric_display[0].precision = 2;
  assert.throws(() => normalizeCandidateResults(unknownField), /metric display fields/i);

  const unknownTopLevel = described();
  unknownTopLevel.metric_scale = 100;
  assert.throws(() => normalizeCandidateResults(unknownTopLevel), /artifact fields/i);
});

test('candidate results reject unknown options and duplicate technical ranks', () => {
  const unknownOption = artifact();
  unknownOption.candidates[0].assignment[0] = 'OPTION_2';
  assert.throws(() => normalizeCandidateResults(unknownOption), /assignment/i);

  const duplicateRank = artifact();
  duplicateRank.candidates[0].metrics.candidate_rank = (
    duplicateRank.candidates[1].metrics.candidate_rank
  );
  assert.throws(() => normalizeCandidateResults(duplicateRank), /candidate rank/i);
});
