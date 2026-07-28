export function workbenchAutoFocus({
  analyzeUnlocked,
  hasTables,
  analyzeDone,
  interviewUnlocked,
  interviewStarted,
  interviewDone,
  generateDone,
  stage,
}) {
  if (!analyzeUnlocked) return 'ingest';
  if (!analyzeDone) return hasTables ? 'analyze' : 'ingest';
  if (!interviewUnlocked || !interviewStarted) return 'analyze';
  if (!interviewDone) return 'interview';
  if (!generateDone) return 'generate';
  if (stage === 'optimize') return 'optimize';
  if (stage === 'evaluate') return 'evaluate';
  if (stage === 'testcases') return 'testcases';
  if (stage === 'data_source') return 'data_source';
  return 'review';
}
