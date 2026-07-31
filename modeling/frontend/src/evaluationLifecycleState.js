const VALID_VIEWS = new Set(['cases', 'run', 'diagnose', 'optimize']);

export function rememberEvaluationView(visited, view) {
  if (!VALID_VIEWS.has(view) || visited.has(view)) return visited;
  const next = new Set(visited);
  next.add(view);
  return next;
}
