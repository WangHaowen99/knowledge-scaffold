export function normalizeOptimizeOperation(operation) {
  return ['diagnose', 'propose', 'apply'].includes(operation) ? operation : '';
}

export function shouldPollOptimizeOperation(operation) {
  return operation === undefined || normalizeOptimizeOperation(operation) !== '';
}
