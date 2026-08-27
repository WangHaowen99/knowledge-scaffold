export const KIND_META = {
  object: { label: '对象', directory: 'objects/' },
  operation: { label: '操作', directory: 'operations/' },
  rule: { label: '规则', directory: 'rules/' },
  metric: { label: '指标', directory: 'metrics/' },
  script: { label: '脚本', directory: 'scripts/' },
  diagram: { label: '流程图', directory: 'diagrams/' },
};

export const FUSION_SCOPE_META = {
  objects: { label: '对象', kind: 'object' },
  operations: { label: '操作', kind: 'operation' },
  rules: { label: '规则', kind: 'rule' },
  metrics: { label: '指标', kind: 'metric' },
  scripts: { label: '脚本', kind: 'script' },
  diagrams: { label: '流程图', kind: 'diagram' },
};

const GATE_LABELS = {
  'lossless-resource-coverage': '来源元素完整覆盖',
  'provenance-ledger-complete': '来源与去向可追溯',
  'reference-integrity': '结构化引用闭合',
  'markdown-contract': '详情与索引一致',
  'package-format': '知识包格式有效',
  'mapping-preservation': '数据映射完整保留',
  'flow-integrity': '流程图结构有效',
  'case-insensitive-key-unique': '元素名称保持唯一',
  'alias-unambiguous': '对象别名无歧义',
  'semantic-plan-resolved': '跨包业务概念已处理',
};

export function planStage(plan) {
  if (!plan) return { label: '尚未建立', tone: 'plain', detail: '先选择至少两个来源包' };
  if (plan.status === 'candidate') return { label: '等待整体确认', tone: 'ok', detail: '候选已生成，当前知识包尚未改变' };
  if (plan.status === 'compiling') return { label: '正在检查融合结果', tone: 'agent', detail: '完成后会进入整体确认' };
  if (plan.status === 'ready') return { label: '可以生成候选', tone: 'accent', detail: '所有元素都已有明确去向' };
  if (plan.status === 'failed') return { label: '本次检查未通过', tone: 'stale', detail: plan.error || '请调整融合方案后重试' };
  return {
    label: `还有 ${Number(plan.unresolvedCount) || 0} 项待确定`,
    tone: 'wait',
    detail: '未确定项不会被静默忽略',
  };
}

export function ledgerEntries(plan) {
  const raw = Array.isArray(plan?.ledger)
    ? plan.ledger
    : Array.isArray(plan?.ledger?.resources) ? plan.ledger.resources : [];
  return raw.map((entry) => ({
    sourceId: entry.sourceRecordId || entry.sourceId,
    kind: entry.kind,
    sourceKey: entry.sourceKey,
    fusedKey: entry.fusedKey || '',
    decision: entry.decision || null,
    included: entry.included !== false && entry.decision !== 'exclude_scope',
    targetSourceId: entry.targetSourceId || entry.canonicalTarget?.sourceRecordId || entry.canonicalTarget?.sourceId || '',
    targetKey: entry.targetKey || entry.canonicalTarget?.key || '',
    reason: entry.reason || entry.decisionReason || '',
    disposition: entry.disposition || '',
    provenance: entry.provenance || '',
  }));
}

export function unresolvedEntries(plan) {
  return ledgerEntries(plan).filter((entry) => !entry.decision);
}

export function dispositionFor(entry, action = 'preserve', details = {}) {
  return {
    sourceId: entry.sourceId,
    kind: entry.kind,
    key: entry.sourceKey,
    action,
    ...(action === 'canonicalize' ? {
      targetSourceId: details.targetSourceId,
      targetKey: details.targetKey,
      reason: details.reason,
    } : action === 'exclude' ? { reason: details.reason } : {}),
  };
}

export function availableFusionScopes(source) {
  const available = new Set(source?.scopes || []);
  return Object.keys(FUSION_SCOPE_META).filter((scope) => available.has(scope));
}

export function fusionScopeCount(source, scope) {
  const count = Number(source?.counts?.[scope]);
  return Number.isFinite(count) && count >= 0 ? count : null;
}

export function fusionScopeProblems(source, selectedScopes) {
  const selected = new Set(selectedScopes || []);
  return (source?.scopeDependencies || []).filter((dependency) => (
    selected.has(dependency.fromScope) && !selected.has(dependency.requiresScope)
  ));
}

export function duplicateFusionPackageIds(sources) {
  const counts = new Map();
  for (const source of sources || []) {
    const id = String(source?.packageId || '').trim();
    if (id) counts.set(id, (counts.get(id) || 0) + 1);
  }
  return new Set([...counts].filter(([, count]) => count > 1).map(([id]) => id));
}

export function fusionConceptGroups(plan) {
  const concepts = Array.isArray(plan?.concepts) ? plan.concepts : [];
  return {
    common: concepts.filter((concept) => concept.layer === 'common'),
    parameterized: concepts.filter((concept) => concept.layer === 'parameterized'),
  };
}

export function qualityRows(plan, failedQuality = null) {
  const quality = plan?.quality || failedQuality;
  if (Array.isArray(quality?.gates)) {
    return quality.gates.map((gate) => ({
      id: gate.id,
      label: GATE_LABELS[gate.id] || '融合质量检查',
      passed: Boolean(gate.passed),
      detail: gate.detail || '',
    }));
  }
  const unresolved = Number(plan?.unresolvedCount) || 0;
  return [
    {
      id: 'destinations',
      label: '每个来源元素都有明确去向',
      passed: plan ? unresolved === 0 : null,
      detail: plan ? (unresolved ? `仍有 ${unresolved} 项待确定` : '逐元素去向账本完整') : '建立方案后检查',
    },
    {
      id: 'compile-checks',
      label: '引用、格式、映射与流程结构',
      passed: null,
      detail: '生成候选时以只读来源重新完整检查',
    },
  ];
}

export function sourceLabel(source) {
  return `${source?.name || source?.filename || '来源知识包'}${source?.version ? ` ${source.version}` : ''}`;
}

export function defaultFusionOutput({ projectId, projectName, packageVersion }) {
  const suffix = String(projectId || 'package').replace(/[^a-zA-Z0-9_-]/g, '').slice(-24) || 'package';
  return {
    id: `fusion-${suffix}`,
    name: `${projectName || '当前项目'}融合知识包`,
    description: '无损整合所选来源，并保留各来源的业务场景视图和可追溯依据。',
    version: packageVersion || '0.1.0',
  };
}
