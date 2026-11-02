// 证据源本期不开放（ADR-0056）：前端按「证据源是否可用」统一过滤露出，不依赖后端碰巧不下发。
// 判断沿用 B5a 的口径：项目内看扩展项有没有数据源子页（随系统逆向建模包出现）；
// 项目外（新建向导）与项目设置看 EV-01 读不读得到（本期回 501）。

// 依赖证据源的包：系统逆向建模（数据源、采集与快照、证据浏览器三个子页都由它带来）
const EVIDENCE_PACKS = new Set(['reverse']);

// 项目内：project 是 useProject() 的值
export const evidenceInProject = (project) => !!project?.hasSubpage?.('sources');

// 项目外：EV-01 的结果（读不到为 null）
export const evidenceFromSources = (sources) => !!sources;

export const isEvidencePack = (name) => EVIDENCE_PACKS.has(name);

// 流程节点：外部节点要证据源提供方；系统逆向建模包的节点（如「系统取证」）
export const isEvidenceNode = (node) => node?.kind === 'external' || isEvidencePack(node?.plugin?.name || node?.pack);

// MCP 服务器与工具：内置的 evidence 服务器、evidence.* 工具
export const isEvidenceMcp = (item) => {
  const id = String(item?.id || item?.name || '');
  return id === 'evidence' || /^evidence[._]/.test(id);
};
