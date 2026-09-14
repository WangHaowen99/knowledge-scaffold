// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
const SCHEMA_VERSION = 'solver.candidate-results.v1';
const OPTIONS = Object.freeze(['OPTION_0', 'OPTION_1']);
const REQUIRED_TOP_LEVEL_FIELDS = Object.freeze([
  'candidate_count',
  'candidate_source_digest',
  'candidate_source_kind',
  'candidates',
  'item_ids',
  'option_labels',
  'presentation_revision',
  'problem_digest',
  'schema_version',
  'selected_candidate_id',
]);
// Artifacts produced before the solver described its metrics omit the field, so it
// stays optional rather than invalidating every stored result.
const OPTIONAL_TOP_LEVEL_FIELDS = Object.freeze(['metric_display']);
const METRIC_DISPLAY_FIELDS = Object.freeze(['key', 'label', 'scale', 'sense', 'unit']);
const METRIC_SENSES = Object.freeze(['maximize', 'minimize']);
const ROW_FIELDS = Object.freeze([
  'assignment',
  'candidate_id',
  'combination_no',
  'eligible',
  'metrics',
  'rejection_reasons',
  'selected',
  'validation_status',
]);

const plainObject = (value) =>
  value != null && typeof value === 'object' && !Array.isArray(value);

const invalid = (message) => {
  const error = new Error(`Invalid candidate results: ${message}`);
  error.code = 'candidate_results_invalid';
  throw error;
};

const exactFields = (value, fields, label) => {
  if (!plainObject(value)) {
    invalid(`${label} must be an object`);
  }
  const actual = Object.keys(value).sort();
  if (
    actual.length !== fields.length ||
    actual.some((key, index) => key !== fields[index])
  ) {
    invalid(`${label} fields are invalid`);
  }
};

const requiredText = (value, label) => {
  if (typeof value !== 'string' || !value.trim()) {
    invalid(`${label} is required`);
  }
  return value.trim();
};

const freezeRow = (row) =>
  Object.freeze({
    ...row,
    assignment: Object.freeze([...row.assignment]),
    metrics: Object.freeze({ ...row.metrics }),
    rejectionReasons: Object.freeze([...row.rejectionReasons]),
    sequenceLabels: Object.freeze([...row.sequenceLabels]),
  });

const artifactFields = (payload) => {
  if (!plainObject(payload)) {
    invalid('artifact must be an object');
  }
  const present = new Set(Object.keys(payload));
  const known = new Set([
    ...REQUIRED_TOP_LEVEL_FIELDS,
    ...OPTIONAL_TOP_LEVEL_FIELDS,
  ]);
  const fieldsMismatch =
    REQUIRED_TOP_LEVEL_FIELDS.some((field) => !present.has(field)) ||
    [...present].some((field) => !known.has(field));
  if (fieldsMismatch) {
    invalid('artifact fields are invalid');
  }
};

const normalizeMetricDisplay = (raw, rows) => {
  if (raw == null) {
    return [];
  }
  if (!Array.isArray(raw)) {
    invalid('metric display is invalid');
  }
  const descriptors = raw.map((descriptor) => {
    exactFields(descriptor, METRIC_DISPLAY_FIELDS, 'metric display');
    if (!Number.isSafeInteger(descriptor.scale) || descriptor.scale < 1) {
      invalid('metric display scale is invalid');
    }
    if (!METRIC_SENSES.includes(descriptor.sense)) {
      invalid('metric display sense is invalid');
    }
    return Object.freeze({
      key: requiredText(descriptor.key, 'metric display key'),
      label: requiredText(descriptor.label, 'metric display label'),
      unit: requiredText(descriptor.unit, 'metric display unit'),
      scale: descriptor.scale,
      sense: descriptor.sense,
    });
  });
  const keys = new Set(descriptors.map((descriptor) => descriptor.key));
  if (keys.size !== descriptors.length) {
    invalid('metric display keys are duplicated');
  }
  // A described metric that some row lacks would render as a blank comparison cell.
  const valueMissing = descriptors.some((descriptor) =>
    rows.some((row) => !Number.isSafeInteger(row.metrics[descriptor.key])),
  );
  if (valueMissing) {
    invalid('metric display value is missing');
  }
  return descriptors;
};

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
export function normalizeCandidateResults(payload) {
  artifactFields(payload);
  if (payload.schema_version !== SCHEMA_VERSION) {
    invalid('schema version is unsupported');
  }
  const problemDigest = requiredText(payload.problem_digest, 'problem digest');
  const candidateSourceKind = requiredText(
    payload.candidate_source_kind,
    'candidate source kind',
  );
  if (candidateSourceKind !== 'problem_ir.candidate_selection') {
    invalid('candidate source kind is unsupported');
  }
  const candidateSourceDigest = requiredText(
    payload.candidate_source_digest,
    'candidate source digest',
  );
  const presentationRevision = requiredText(
    payload.presentation_revision,
    'presentation revision',
  );
  if (!Array.isArray(payload.item_ids) || payload.item_ids.length === 0) {
    invalid('item IDs are invalid');
  }
  const itemIds = payload.item_ids.map((value) =>
    requiredText(value, 'item ID'),
  );
  if (new Set(itemIds).size !== itemIds.length) {
    invalid('item IDs are duplicated');
  }
  exactFields(payload.option_labels, OPTIONS, 'option labels');
  const optionLabels = Object.freeze(
    Object.fromEntries(
      OPTIONS.map((option) => [
        option,
        requiredText(payload.option_labels[option], option),
      ]),
    ),
  );
  const candidateCount = payload.candidate_count;
  if (!Number.isSafeInteger(candidateCount) || candidateCount < 1) {
    invalid('candidate count is invalid');
  }
  if (
    !Array.isArray(payload.candidates) ||
    payload.candidates.length !== candidateCount
  ) {
    invalid('candidate count does not match content');
  }

  const rows = payload.candidates
    .map((candidate) => {
      exactFields(candidate, ROW_FIELDS, 'candidate');
      const combinationNo = candidate.combination_no;
      if (!Number.isSafeInteger(combinationNo) || combinationNo < 1) {
        invalid('combination number is invalid');
      }
      const candidateId = requiredText(candidate.candidate_id, 'candidate ID');
      if (
        !Array.isArray(candidate.assignment) ||
        candidate.assignment.length !== itemIds.length
      ) {
        invalid('candidate assignment is invalid');
      }
      const assignment = candidate.assignment.map((option) => {
        if (!OPTIONS.includes(option)) {
          invalid('candidate assignment contains an unknown option');
        }
        return option;
      });
      if (typeof candidate.eligible !== 'boolean') {
        invalid('candidate eligibility is invalid');
      }
      const expectedStatus = candidate.eligible ? 'feasible' : 'infeasible';
      if (candidate.validation_status !== expectedStatus) {
        invalid('candidate validation status is inconsistent');
      }
      if (!plainObject(candidate.metrics)) {
        invalid('candidate metrics are invalid');
      }
      const candidateRank = candidate.metrics.candidate_rank;
      if (!Number.isSafeInteger(candidateRank) || candidateRank < 0) {
        invalid('candidate rank is invalid');
      }
      if (
        !Array.isArray(candidate.rejection_reasons) ||
        candidate.rejection_reasons.some(
          (reason) => typeof reason !== 'string' || !reason.trim(),
        )
      ) {
        invalid('candidate rejection reasons are invalid');
      }
      if (typeof candidate.selected !== 'boolean') {
        invalid('candidate selected flag is invalid');
      }
      return freezeRow({
        combinationNo,
        candidateId,
        assignment,
        eligible: candidate.eligible,
        validationStatus: candidate.validation_status,
        metrics: candidate.metrics,
        rejectionReasons: candidate.rejection_reasons.map((reason) =>
          reason.trim(),
        ),
        selected: candidate.selected,
        sequenceLabels: assignment.map((option) => optionLabels[option]),
      });
    })
    .sort((left, right) => left.combinationNo - right.combinationNo);

  if (rows.some((row, index) => row.combinationNo !== index + 1)) {
    invalid('combination numbers are incomplete or duplicated');
  }
  if (new Set(rows.map((row) => row.candidateId)).size !== candidateCount) {
    invalid('candidate IDs are duplicated');
  }
  const ranks = rows
    .map((row) => row.metrics.candidate_rank)
    .sort((a, b) => a - b);
  if (ranks.some((rank, index) => rank !== index)) {
    invalid('candidate ranks are incomplete or duplicated');
  }
  const selectedCandidateId =
    payload.selected_candidate_id == null ?
      null :
      requiredText(payload.selected_candidate_id, 'selected candidate ID');
  const selectedRows = rows.filter((row) => row.selected);
  const selectedCandidateMismatch =
    (selectedCandidateId == null && selectedRows.length !== 0) ||
    (selectedCandidateId != null &&
      (selectedRows.length !== 1 ||
        selectedRows[0].candidateId !== selectedCandidateId));
  if (selectedCandidateMismatch) {
    invalid('selected candidate does not match content');
  }

  return Object.freeze({
    schemaVersion: SCHEMA_VERSION,
    problemDigest,
    candidateSourceKind,
    candidateSourceDigest,
    presentationRevision,
    itemIds: Object.freeze([...itemIds]),
    optionLabels,
    candidateCount,
    selectedCandidateId,
    rows: Object.freeze(rows),
    metricDisplay: Object.freeze(normalizeMetricDisplay(payload.metric_display, rows)),
  });
}
