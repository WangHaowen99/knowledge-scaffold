export function normalizeOptimizeOperation(operation) {
  return operation === 'propose' || operation === 'apply' ? operation : '';
}

export function shouldPollOptimizeOperation(operation) {
  return operation === undefined || normalizeOptimizeOperation(operation) !== '';
}
