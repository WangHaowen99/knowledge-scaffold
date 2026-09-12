const SOLVER_STORAGE_PREFIX = 'mw_solver_';
const SUPPORTED_OPTIONS = Object.freeze(['OPTION_0', 'OPTION_1']);

export const SOLVER_VALIDATOR_REVISION = 'ontology_operation_batch.v1';

export function createValidationIdempotencyKey(
  randomId = () =>
    globalThis.crypto?.randomUUID?.() ||
    `${Date.now()}-${Math.random().toString(16).slice(2)}`,
) {
  return `solver-validation-${requiredText(randomId(), '随机标识')}`;
}

export function createRequestEpochGuard() {
  let epoch = 0;
  return {
    capture: () => epoch,
    invalidate: () => {
      epoch += 1;
      return epoch;
    },
    accepts: (candidateEpoch) => candidateEpoch === epoch,
  };
}

export function createValidationAttemptKeyManager(
  createKey = createValidationIdempotencyKey,
) {
  let activeKey = '';
  return {
    begin: () => {
      activeKey = requiredText(createKey(), '校验请求幂等键');
      return activeKey;
    },
    retry: () => {
      if (!activeKey) {
        activeKey = requiredText(createKey(), '校验请求幂等键');
      }
      return activeKey;
    },
    complete: () => {
      activeKey = '';
    },
    reset: () => {
      activeKey = '';
    },
  };
}

const cleanText = (value) => String(value ?? '').trim();

const requiredText = (value, label) => {
  const normalized = cleanText(value);
  if (!normalized) {
    throw new Error(`${label}不能为空`);
  }
  return normalized;
};

const integerValue = (value, label, { minimum = 0 } = {}) => {
  if (typeof value === 'boolean' || cleanText(value) === '') {
    throw new Error(`${label}必须是整数`);
  }
  const normalized = Number(value);
  if (!Number.isInteger(normalized)) {
    throw new Error(`${label}必须是整数`);
  }
  if (normalized < minimum) {
    throw new Error(`${label}不能小于 ${minimum}`);
  }
  return normalized;
};

const positiveDecimal = (value, label) => {
  const normalized = cleanText(value);
  if (!/^[0-9]+(?:\.[0-9]+)?$/.test(normalized) || Number(normalized) <= 0) {
    throw new Error(`${label}必须是大于 0 的数字`);
  }
  return normalized;
};

const normalizeOptions = (value) => {
  const options = (
    Array.isArray(value) ? value : cleanText(value).split(/[\s,，]+/)
  )
    .map(cleanText)
    .filter(Boolean);
  if (
    options.length !== SUPPORTED_OPTIONS.length ||
    new Set(options).size !== options.length ||
    options.some((option, index) => option !== SUPPORTED_OPTIONS[index])
  ) {
    throw new Error('候选选项必须按顺序填写 OPTION_0、OPTION_1，且不能重复');
  }
  return [...options];
};

export function createDefaultPlan() {
  return {
    knowledge_package_id: '',
    knowledge_package_revision: '',
    option_domain: [...SUPPORTED_OPTIONS],
    max_transitions: 1,
    items: [
      {
        item_order: 1,
        item_id: '',
        quantity: '1',
        duration: '1',
      },
    ],
  };
}

export function normalizePlan(input = {}) {
  const items = Array.isArray(input.items) ? input.items : [];
  if (!items.length) {
    throw new Error('计划至少需要一个条目');
  }
  if (items.length > 64) {
    throw new Error('计划最多支持 64 个条目');
  }

  const normalizedItems = items.map((item, index) => ({
    item_order: integerValue(item?.item_order, `第 ${index + 1} 行顺序`, {
      minimum: 1,
    }),
    item_id: requiredText(item?.item_id, `第 ${index + 1} 行条目 ID`),
    quantity: positiveDecimal(item?.quantity, `第 ${index + 1} 行数量`),
    duration: positiveDecimal(item?.duration, `第 ${index + 1} 行时长`),
  }));
  const expectedOrders = normalizedItems.map((_, index) => index + 1);
  if (
    normalizedItems.some(
      (item, index) => item.item_order !== expectedOrders[index],
    )
  ) {
    throw new Error('条目顺序必须从 1 开始连续递增');
  }
  if (
    new Set(normalizedItems.map((item) => item.item_id)).size !==
    normalizedItems.length
  ) {
    throw new Error('条目 ID 不能重复');
  }

  const maxTransitions = integerValue(input.max_transitions, '最大切换次数');
  if (maxTransitions !== 1) {
    throw new Error('最大切换次数必须为 1');
  }

  return {
    knowledge_package_id: requiredText(input.knowledge_package_id, '知识包 ID'),
    knowledge_package_revision: requiredText(
      input.knowledge_package_revision,
      '知识包版本',
    ),
    plan: {
      option_domain: normalizeOptions(input.option_domain),
      max_transitions: maxTransitions,
      items: normalizedItems,
    },
  };
}

export const solverStorageKey = (workspaceId) =>
  `${SOLVER_STORAGE_PREFIX}${cleanText(workspaceId)}`;

const emptyRunIds = () => ({ solveRunId: '', validationRunId: '' });

export function readSolverRunIds(storage, workspaceId) {
  if (!storage || !cleanText(workspaceId)) {
    return emptyRunIds();
  }
  try {
    const parsed = JSON.parse(
      storage.getItem(solverStorageKey(workspaceId)) || '{}',
    );
    return {
      solveRunId: cleanText(parsed?.solve_run_id),
      validationRunId: cleanText(parsed?.validation_run_id),
    };
  } catch {
    return emptyRunIds();
  }
}

export function writeSolverRunIds(storage, workspaceId, runIds = {}) {
  if (!storage || !cleanText(workspaceId)) {
    return false;
  }
  const solveRunId = cleanText(runIds.solveRunId);
  const validationRunId = cleanText(runIds.validationRunId);
  try {
    if (!solveRunId && !validationRunId) {
      storage.removeItem(solverStorageKey(workspaceId));
    } else {
      storage.setItem(
        solverStorageKey(workspaceId),
        JSON.stringify({
          solve_run_id: solveRunId,
          validation_run_id: validationRunId,
        }),
      );
    }
    return true;
  } catch {
    return false;
  }
}

export function isCompleteCandidateArtifact(artifact) {
  const candidates = Array.isArray(artifact?.candidates) ?
    artifact.candidates :
    [];
  return Boolean(
    cleanText(artifact?.source_run_id) &&
      cleanText(artifact?.candidate_set_ref) &&
      cleanText(artifact?.candidate_set_digest) &&
      Number.isInteger(artifact?.candidate_count) &&
      artifact.candidate_count > 0 &&
      candidates.length === artifact.candidate_count &&
      candidates.every(
        (candidate) =>
          cleanText(candidate?.candidate_id) &&
          cleanText(candidate?.candidate_digest),
      ),
  );
}

export function buildValidationPayload(
  candidateArtifact,
  validatorRevision = SOLVER_VALIDATOR_REVISION,
) {
  if (!isCompleteCandidateArtifact(candidateArtifact)) {
    throw new Error('候选集制品不完整，无法发起校验');
  }
  const sourceRunId = cleanText(candidateArtifact.source_run_id);
  return {
    source_run_id: sourceRunId,
    parent_solve_run_id: sourceRunId,
    candidate_set_ref: cleanText(candidateArtifact.candidate_set_ref),
    candidate_set_digest: cleanText(candidateArtifact.candidate_set_digest),
    validator_revision: requiredText(validatorRevision, '校验器版本'),
  };
}
