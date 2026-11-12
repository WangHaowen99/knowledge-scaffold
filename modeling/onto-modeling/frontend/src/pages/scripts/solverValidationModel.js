// 求解验证卡的纯逻辑（develop SolverModelPanel 的 ValidationStatusCard 文字与判定，ADR-0098）

export const shortRevision = (revision) => (revision ? String(revision).replace(/^sha256:/, '').slice(0, 10) : '—');

// 当前修订是否要在发布后验证：启用了求解契约且有启用的求解操作
export function needsValidation(view) {
  return Boolean(view?.enabled && (view.operations || []).some((operation) => operation.enabled !== false));
}

export function validationText({ status, dirty, current }) {
  if (dirty) return '有未保存的修改：保存并发布新版本后，再对新版本发起求解验证。';
  if (status?.configured === false) return '评测服务不可用或未配置，暂时读不到求解验证状态。';
  if (current.state === 'passed') return `当前修订已在 ${current.version} 通过求解验证。`;
  if (current.state === 'published') return '当前修订已发布，尚未通过求解验证：在评测页面对该版本发起求解验证。';
  if (status) return '当前修改尚未发布和验证：发布新版本后，在「已发布版本（知识包评测）」中发起求解验证。';
  return '正在读取求解验证状态。';
}
