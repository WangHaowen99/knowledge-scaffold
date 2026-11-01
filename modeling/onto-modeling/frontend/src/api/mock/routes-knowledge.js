// mock · 3.8 知识（K-01～K-08）：知识沉淀页、对话里「沉淀为业务知识」
export function register({ on, json, err, uid }) {
  on('GET', '/projects/([^/]+)/knowledge/conflicts', () => json(200, { checked_at: new Date().toISOString(), duration_ms: 120, total_entries: 12, conflicts: [], consistent_with_materials: 10, unsourced: 1, duplicates: 0, broken_refs: 0 }));
  on('POST', '/projects/([^/]+)/knowledge', ({ body }) => {
    if (!body.title || !body.rule_md) return err(400, 'invalid_argument', '缺少内容', { fields: { title: 'required' } });
    return json(201, { entry: { id: `kn-${uid()}`, title: body.title, layer: 'project', pack: null, type: body.type, scope: body.scope, confidence: body.confidence || 'medium', source: { kind: 'conversation', label: '对话' }, overrides_industry: false, readonly: false, revision: 'r1', updated_at: new Date().toISOString() }, commit: 'k1', version_label: 'v0.13', conflicts: [], review_task: null });
  });
}
