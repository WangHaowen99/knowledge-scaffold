export function canProposeFromDiagnoses(diagnoses = []) {
  return diagnoses.some((diagnosis) => diagnosis.resolutionStatus === 'confirmed'
    && diagnosis.packageOptimizable
    && !diagnosis.blocking);
}

export function diagnosesForRound(diagnoses = [], roundId) {
  return diagnoses.filter((diagnosis) => diagnosis.roundId === roundId);
}

export function suggestionsForRound(suggestions = [], roundId) {
  if (!roundId) return [];
  return suggestions.filter((suggestion) => suggestion.roundId === roundId);
}

export function pendingSuggestionIdsForRound(suggestions = [], roundId) {
  return suggestionsForRound(suggestions, roundId)
    .filter((suggestion) => suggestion.status === 'pending')
    .map((suggestion) => suggestion.id);
}

export function optimizationExpertReviewState(value) {
  if (value?.status === 'waiting_expert') {
    const taskId = String(value.taskId ?? '').trim();
    if (!taskId) return null;
    return {
      taskId,
      suggestionIds: Array.isArray(value.suggestionIds) ? value.suggestionIds.map(String) : [],
      validationErrors: Array.isArray(value.validationErrors) ? value.validationErrors.map(String) : [],
      error: String(value.error ?? ''),
    };
  }
  const node = value?.task?.node;
  const state = node?.output;
  if (node?.status !== 'waiting_user' || state?.phase !== 'expert_review') return null;
  return {
    taskId: String(node.id),
    suggestionIds: Array.isArray(state.suggestionIds) ? state.suggestionIds.map(String) : [],
    validationErrors: Array.isArray(state.validationErrors) ? state.validationErrors.map(String) : [],
    error: String(state.error ?? ''),
  };
}

export function optimizationExpertReviewForRound(value, suggestions = [], roundId) {
  const review = optimizationExpertReviewState(value);
  if (!review || !roundId || !review.suggestionIds.length) return null;
  const roundSuggestionIds = new Set(suggestionsForRound(suggestions, roundId)
    .map((suggestion) => suggestion.id));
  return review.suggestionIds.every((id) => roundSuggestionIds.has(id)) ? review : null;
}

export async function diagnoseAndRefresh(runDiagnosis, refreshState) {
  await runDiagnosis();
  return refreshState();
}

export function requireOverrideReason(value) {
  const reason = String(value ?? '').trim();
  if (!reason) throw new Error('请填写人工改判理由');
  return reason;
}
