export function summarizeEvaluationCases(cases = []) {
  const official = cases.filter((item) => item.source === 'manual');
  const suggestions = cases.filter((item) => item.source !== 'manual');
  const ready = official.filter((item) => String(item.groundTruth ?? '').trim());
  return {
    official,
    suggestions,
    officialTotal: official.length,
    officialReady: ready.length,
    withoutReferenceTotal: official.length - ready.length,
    suggestionTotal: suggestions.length,
    canRun: official.length > 0,
  };
}
