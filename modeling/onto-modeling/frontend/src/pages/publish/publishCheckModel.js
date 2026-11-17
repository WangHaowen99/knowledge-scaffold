// 发布检查与已发布版本的纯逻辑：文字照 develop（PublishCheckDialog、PublishedVersionsDialog、lib/packagePublish.js），
// 数据取 PB-01（stages、solver、structure、binding、last_publish）与 HB-05（Hub 上这个包的版本）。

// 发布必须完成的建模阶段（与后端 publish/stages.py 同一规则；后端已算好，这里只组织提示）
export const PUBLISH_REQUIRED_STAGES = ['research', 'object', 'operation', 'validate'];
export const missingPublishStages = (check) => check?.stages?.missing_stages || [];
export function stageNote(check) {
  const missing = missingPublishStages(check);
  return missing.length ? `建模未完成，请先完成：${missing.map((stage) => stage.title).join('、')}` : '';
}

export const KIND_TEXT = {
  declared_missing: '声明的表不存在',
  no_columns: '表里没有一列对得上',
  declared_unverifiable: '无数据表可核对',
};

export const STRUCTURE_TITLES = {
  package_archive_invalid: '压缩包',
  package_manifest_invalid: '清单',
  package_revision_invalid: '修订包',
  package_limit_exceeded: '数量上限',
  object_aliases_invalid: '对象别名',
  field_aliases_invalid: '字段别名',
  solver_package_unsafe: '求解包安全',
  solver_layout_invalid: '求解布局',
  package_export_failed: '导出失败',
};

export const FINDING_TITLES = {
  // 该错误码同时覆盖不支持的 Profile 与评估器不符合 Consumer 兼容层接口，标题不能只说版本
  solver_contract_version_incompatible: '契约不兼容',
  solver_contract_reference_missing: '引用缺失',
  solver_context_missing: 'SOLVER_CONTEXT 不完整',
  solver_runtime_binding_forbidden: '含运行时绑定',
  solver_contract_invalid: '契约不合法',
  solver_contract_revision_conflict: '修订冲突',
};

export const shortRevision = (revision) => (revision ? String(revision).replace(/^sha256:/, '').slice(0, 10) : '—');
export const findingSource = (finding) => [finding?.path, finding?.field, finding?.code].filter(Boolean).join(' · ');

// 「打开文档 / 打开文件」：本体文档到 Studio 的条目，脚本到代码生成页；求解契约受管目录不出入口
export function fileTarget(path) {
  const p = String(path || '');
  const entry = p.match(/^ontology\/(object|operation|logic|rule|metric)\/([^/]+)\.md$/);
  if (entry) return { page: 'studio', query: { entry: `${entry[1] === 'logic' ? 'operation' : entry[1]}:${entry[2]}` } };
  if (/^ontology\/scripts\//.test(p)) return { page: 'scripts' };
  return null;
}

// 不以空格结尾：发到当前对话里直接执行
export function assistantPrompt(issues) {
  return [
    '发布前数据绑定检查发现以下对象的「# 数据来源」与项目数据表对不上，请逐个修正：',
    ...issues.map((issue) => `- ${issue.message}`),
    '修正口径：首行只写现有数据表名（schema 表格资料里的 Sheet 名，逐字相同）；'
      + '确实不落库的对象填「无」；不要臆造表名。修完运行规则检查，确认没有「列名对账」错误。',
  ].join('\n');
}

// 「改为现有数据表」：推荐（带对得上的列数）在前，其余在「全部数据表」
export function tableGroups(issue, tables) {
  const candidates = issue.candidates || [];
  const recommended = new Set(candidates.map((item) => item.table));
  const groups = [];
  if (candidates.length) {
    groups.push({ label: '推荐', options: candidates.map((item) => ({ value: item.table, label: item.matched ? `${item.table}（${item.matched} 列对得上）` : item.table })) });
  }
  groups.push({ label: '全部数据表', options: (tables || []).filter((name) => !recommended.has(name)).map((name) => ({ value: name, label: name })) });
  return groups;
}

const gateBlocking = (check, id) => Boolean((check?.gates || []).find((g) => g.id === id)?.blocking);
// 降级不能绕过的问题数：求解契约静态检查与结构自检。渲染时才发现的结构问题（关系写法、脚本绑定）只在门禁里，
// 不计入 structure.blocking，也要挡住
export const hardBlockers = (check) => (check?.solver?.blocking || 0)
  + Math.max(check?.structure?.blocking || 0, gateBlocking(check, 'package_structure') ? 1 : 0);
export const needsSolverAcknowledgement = (check) => Boolean(check?.solver?.requires_validation_after_publish);

// 「发布」按钮：develop 的三种说法；建模阶段没做完时置灰，说明同 develop 的提示
export function proceedState(check, acknowledged) {
  const blocking = (check?.binding?.issues || []).filter((issue) => issue.blocking).length;
  const hard = hardBlockers(check);
  const awaiting = needsSolverAcknowledgement(check) && !acknowledged;
  const stages = stageNote(check);
  const otherGates = (check?.gates || []).filter((g) => g.blocking && !['data_binding', 'solver_contract', 'package_structure', 'modeling_stages'].includes(g.id));
  let label = '发布';
  if (blocking || hard) label = '发布（需先修复）';
  else if (awaiting) label = '发布（需先确认）';
  const disabled = !check || !!stages || blocking > 0 || hard > 0 || awaiting || otherGates.length > 0;
  const title = stages || (otherGates.length ? `${otherGates.map((g) => g.title).join('、')}未通过` : '');
  return { label, disabled, title };
}

// 降级：只放宽数据绑定；求解契约、知识包结构、建模阶段与其他门禁要先过
export function degradeState(check, acknowledged) {
  const others = (check?.gates || []).filter((g) => g.blocking && !g.degradable);
  const awaiting = needsSolverAcknowledgement(check) && !acknowledged;
  return {
    disabled: others.length > 0 || awaiting,
    title: hardBlockers(check) > 0 ? '降级只放宽数据绑定，求解契约与知识包结构问题须先修正' : (others.length ? `${others.map((g) => g.title).join('、')}未通过` : ''),
  };
}
export const DEGRADE_NOTE = '这些对象发布后不绑定数据表，发布记录会注明降级发布。';

// 发布成功的提示（develop usePackagePublish）
export function publishedText(release, { degrade = false, acknowledged = false } = {}) {
  if (release.local_record_pending) return { tone: 'warning', text: '知识包已发布，本地发布记录待恢复' };
  if (acknowledged) return { tone: 'success', text: `知识包 ${release.version} 已发布，求解能力待验证`, evaluate: true };
  return { tone: 'success', text: degrade ? '已降级发布，未通过检查的对象不绑定数据表' : '知识包已发布' };
}

// ---------- 已发布版本（HB-05） ----------
export const VERSIONS_PAGE = 10;
export { EVALUATION_HINT } from '../evaluation/evaluationModel.js';
// 本服务（modeling）发布的不写来源，只写别的服务（如评测回写）的
export const releaseSource = (row) => (row?.source_service && row.source_service !== 'modeling' ? row.source_service : '');

// 评测优化之后发布：回写评测服务这是哪一轮优化建议的处理结果（develop usePackagePublish 的两句提示）
export function writebackText(release) {
  const w = release?.evaluation_writeback;
  if (w?.status === 'done' && w.release_id === (release.hub?.release_id || release.release_id)) {
    return { tone: 'success', text: `已回写评测服务：新版本 ${w.version} 处理了 ${w.base_version} 的优化建议` };
  }
  if (w?.status === 'pending') return { tone: 'warning', text: '新版本的处理结果暂未回写评测服务，打开已发布版本时会自动重试' };
  return null;
}

// ---------- 发布页：结论、检查项、需要处理 ----------

// 页面顶上的一句结论：能不能发、卡在哪几项（门禁的 blocking 是唯一口径）
export function publishVerdict(check, { versionLabel = '', acknowledged = false } = {}) {
  if (!check) return { tone: 'idle', title: '正在检查…', sub: '' };
  const label = versionLabel ? ` ${versionLabel}` : '';
  const blocking = (check.gates || []).filter((g) => g.blocking);
  if (blocking.length) return { tone: 'bad', title: `还不能发布${label}`, sub: `${blocking.map((g) => g.title).join('、')}没通过` };
  if (needsSolverAcknowledgement(check) && !acknowledged) {
    return { tone: 'warn', title: `可以发布${label}`, sub: '求解能力还没验证，在下面「求解契约」里勾选确认后才能发布' };
  }
  const notes = (check.gates || []).filter((g) => g.status !== 'pass');
  return { tone: 'ok', title: `可以发布${label}`, sub: notes.length ? `${notes.map((g) => g.title).join('、')}有提醒，不挡发布` : '' };
}

// 检查项小标签：通过 / 有提醒 / 挡住（还没运行的流程关卡算挡住或提醒，看它挡不挡发布）
export function gateMark(g) {
  if (g.blocking || g.status === 'fail') return { tone: 'bad', icon: 'x', text: '没通过' };
  if (g.status === 'pass') return { tone: 'ok', icon: 'check', text: '通过' };
  return { tone: 'warn', icon: 'alert', text: g.status === 'warn' ? '有提醒' : '还没运行' };
}

// 「需要处理」列哪些检查项：没通过的在前，有提醒的在后
export const problemGates = (gates) => (gates || []).filter((g) => g.status !== 'pass')
  .sort((a, b) => Number(b.blocking) - Number(a.blocking));

// 知识包结构的问题按对象列：对象名、问题、打开文档的路径（路径与行号只给「打开」和助手用，不显示）
export function structureRows(findings) {
  return (findings || []).map((f, i) => ({
    key: `${f.path || ''}:${f.line || ''}:${i}`,
    name: f.target?.name || '',
    message: f.message,
    path: f.path || '',
    line: f.line || null,
  }));
}

// 交给助手修结构问题：带上文件与行号（助手要定位），不以空格结尾
export function structurePrompt(findings) {
  return [
    '发布前的知识包结构检查没通过，请逐条修正：',
    ...(findings || []).map((f) => `- ${f.path ? `${f.path}${f.line ? ` 第 ${f.line} 行` : ''}：` : ''}${f.message}`),
    '修完运行规则检查，确认没有新的结构问题。',
  ].join('\n');
}

// 发布哪个版本的下拉：版本号（与顶栏「知识包 vX」同一口径），当前的标出来
export const iterationOptions = (items) => (items || []).map((v) => ({ value: v.commit, label: `${v.version_label}${v.current ? '（当前）' : ''}` }));

// 已发布版本一张表：Hub 的版本（权威，含别人发布到同一个包的）挂上本地发布记录（降级、包清单、图模式下载）；
// Hub 读不到时退回本地记录
export function releaseRows(hubRows, records) {
  const byId = new Map();
  const byVersion = new Map();
  (records || []).forEach((r) => {
    if (r.hub?.release_id) byId.set(r.hub.release_id, r);
    byVersion.set(r.version, r);
  });
  return (hubRows || []).map((row) => ({ ...row, local: byId.get(row.release_id) || byVersion.get(row.version) || null }));
}
export const localReleaseRows = (records) => (records || []).map((r) => ({
  release_id: r.hub?.release_id || '', version: r.version, created_at: r.at, local: r,
}));

