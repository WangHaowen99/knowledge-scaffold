// 新建项目请求体（契约 P-02）
export function createBody(s) {
  return {
    name: s.name.trim(),
    goal: s.goal.trim(),
    starting_points: s.starts,
    materials: s.files.filter((f) => f.upload).map((f) => ({ upload_id: f.upload.upload_id, purpose: f.purpose || 'auto' })),
    industry_packs: s.industries.map((name) => ({ name })),
    system_packs: s.systems.map((name) => ({ name })),
    capabilities: s.caps,
    engine: s.engine,
    build_mode: s.buildMode,
    ...(s.starts.includes('example') ? { example: { id: 'seed_example' } } : {}),
  };
}
