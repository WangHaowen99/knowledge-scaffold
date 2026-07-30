export function canProposeFromDiagnoses(diagnoses = []) {
  return diagnoses.some((diagnosis) => diagnosis.packageOptimizable && !diagnosis.blocking);
}

export function diagnosesForRound(diagnoses = [], roundId) {
  return diagnoses.filter((diagnosis) => diagnosis.roundId === roundId);
}

export function requireOverrideReason(value) {
  const reason = String(value ?? '').trim();
  if (!reason) throw new Error('请填写人工改判理由');
  return reason;
}
