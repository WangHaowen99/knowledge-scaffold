export function followUpStepForRound(round) {
  if (!round) return '';
  if (round.status === 'failed') return 'diagnose';
  if (round.status === 'done' && Number(round.passCount) < Number(round.progressTotal)) return 'diagnose';
  return '';
}

export function latestDiagnosableRound(rounds = []) {
  return (rounds ?? []).find((round) => followUpStepForRound(round) === 'diagnose') ?? null;
}
