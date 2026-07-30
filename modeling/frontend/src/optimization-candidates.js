export function optimizationCandidateNotice(response) {
  const candidateId = String(response?.candidateId ?? '').trim();
  const status = String(response?.status ?? '').trim();
  const basedRevisionOid = String(response?.basedRevisionOid ?? '').trim();
  const selected = Number(response?.selected);
  if (!candidateId || !status || !basedRevisionOid || !Number.isInteger(selected) || selected < 1) {
    throw new Error('优化候选响应不完整');
  }
  return { candidateId, status, basedRevisionOid, selected };
}

export function linkedOptimizationCandidates(suggestions = [], candidates = []) {
  const linkedIds = new Set((suggestions ?? [])
    .map((suggestion) => suggestion.candidateRevisionId)
    .filter(Boolean));
  return (candidates ?? []).filter((candidate) => linkedIds.has(candidate.id));
}

export function pendingLinkedOptimizationCandidates(suggestions = [], candidates = []) {
  const pendingIds = new Set((suggestions ?? [])
    .filter((suggestion) => suggestion.status === 'candidate')
    .map((suggestion) => suggestion.candidateRevisionId)
    .filter(Boolean));
  return (candidates ?? []).filter((candidate) => candidate.status === 'pending'
    && pendingIds.has(candidate.id));
}
