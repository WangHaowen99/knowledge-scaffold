// mock：插件目录、详情、兼容检查、申请与平台安装（PL-01～PL-04、PL-19）。页面：plugins、plugin-detail、create（第 3 步）
// PL-05～PL-11（按项目启用、升级、回滚、停用）在 routes-settings.js。带 project_id 时经 ctx.self 转调 PL-05 算本项目视角的状态，
// 插件中心和项目设置的能力表说的是同一件事；routes-settings.js 留了两处接口：db.seedProjectPacks（初始能力表）、db.packCatalog（PL-06 查目录）。
// mock 状态 ?mock=plugins:<标记>[+<标记>]：
//   norec  PL-01 不给推荐（B3a 后端现状：推荐恒为空，整条推荐带不出现）
//   held   基础建模 1.5 还在 beta，场景模拟 0.10.0 依赖它：兼容检查不通过，本项目也没排这次升级（设计稿 plugins、plugin-detail 那一刻）
//   ind    零售连锁 1.1.0 已发布到 stable，本项目可以更新（行业包的更新弹窗）
import { industryEntries, projectEntries } from './knowledge-data.js';
import { seedSnapshots } from './evidence-data.js';
import { addAdminRequest } from './routes-pack-uploads.js';
import {
  APPLIES_TO, catalogCards, catalogCounts, compareVersions, detailOf, filterCatalog, installedProviders, isInstalledStatus,
  pluginFlags, projectCard, projectStatsOf, recommendationsOf, requiresCore, satisfies, KERNEL_VERSION,
} from './plugins-data.js';
import { ENTRIES, FLOWCHARTS } from './scenario.js';
import { scripts } from './scripts-data.js';

const CATEGORIES = ['capability', 'industry', 'system', 'provider', 'node', 'exporter'];
const DATA_CATEGORIES = ['industry', 'system'];
const DEV_SKILL_CATEGORIES = ['capability', 'industry', 'system', 'provider'];
const KIND_LABEL = { object: '对象', operation: '操作', rule: '规则', metric: '指标', script: '脚本' };

// 生效中（不算待生效的停用）
const activeNames = (items) => items.filter((p) => ['required', 'enabled', 'pending'].includes(p.status) && p.pending?.kind !== 'disable').map((p) => p.name);

// 「会带来什么」：启用时按包含什么数，升级时用这个版本的更新说明
function bringsText(card, d, upgradeTo) {
  if (upgradeTo) {
    if (d.upgrade_brings) return d.upgrade_brings;
    const c = d.changelog.find((x) => x.version === upgradeTo);
    return c ? `- ${c.notes}` : '';
  }
  if (d.brings) return d.brings;
  const parts = d.contents
    .filter((g) => !['加进流程', '界面入口'].includes(g.group))
    .map((g) => {
      const n = g.total ?? g.items.length;
      if (g.group === '表字典') return `表 ${n} 张`;
      return `${g.group} ${n} ${['术语', '规则', '检查规则', '状态码'].includes(g.group) ? '条' : '个'}`;
    });
  if (d.flow_insertions.length) parts.push(`流程步骤 ${d.flow_insertions.length} 个`);
  return parts.length ? `${parts.join('、')}。` : `「${card.label}」的内容在对话和 Studio 里按需读取。`;
}

function uiEffect(card) {
  const ui = card.ui;
  if (ui?.kind === 'page') return { kind: 'page', text: `启用后左栏出现${ui.pages.map((p) => `「${p.title}」`).join('')}` };
  if (ui?.kind === 'subpages') return { kind: 'subpages', text: ui.note };
  if (ui?.kind === 'kernel') return { kind: 'kernel', text: ui.note };
  if (ui?.kind === 'none') return { kind: 'none', text: (ui.note || '').replace(/^无独立页面\s*[·，,]\s*/, '') || '能力体现在助手对话和 Studio 里' };
  return { kind: 'none', text: card.category === 'system' ? '表字段字典在资料分析和对账时按需读取' : '行业知识在对话和 Studio 里按需读取' };
}

/** PL-03 的结果（纯函数，单测直接调） */
export function compatibilityOf(card, version, d, { items, flags }) {
  const coreLocked = items.find((p) => p.name === 'core')?.locked_version || '1.4.2';
  const range = requiresCore(card.name, version, flags);
  const depOk = !range || satisfies(coreLocked, range);
  const others = activeNames(items).filter((n) => n !== card.name).length;
  const pp = items.find((p) => p.name === card.name);
  const upgrading = !!pp && pp.status !== 'disabled' && compareVersions(version, pp.locked_version) > 0;
  const ins = d.flow_insertions;
  const pages = card.ui?.kind === 'page' ? card.ui.pages : [];
  const subpages = card.ui?.kind === 'subpages' ? card.ui.subpages : [];
  const checks = [
    { id: 'kernel_api', label: '内核接口 kernel_api ">=1.0 <2.0"', ok: true, detail: `内核 ${KERNEL_VERSION}` },
    { id: 'engines', label: '会话引擎', ok: true, detail: '稳定引擎、内测引擎都能用' },
    {
      id: 'depends', label: range ? `依赖 core "${range}"` : '依赖', ok: depOk,
      detail: !range ? '没有依赖' : depOk ? `已锁定 core@${coreLocked}` : '基础建模能力的新版本正在小范围试用，管理员推广后就可以一起更新。',
    },
    { id: 'names', label: '命令、子代理、脚本名唯一', ok: true, detail: `和已启用的 ${others} 个包无冲突` },
    { id: 'flow', label: '流程组装', ok: true, detail: ins.length ? `新增${ins.map((x) => `${x.kind || '节点'}「${x.short || x.title}」`).join('、')}，无环、输出路径不重复` : '不改流程' },
    { id: 'page_api', label: '页面接口 page_api ">=1.0 <2.0"', ok: true, detail: pages.length || subpages.length ? '平台内置页面接口 1.0 支持' : '不带页面' },
  ];
  const cnt = (g) => d.contents.find((c) => c.group === g)?.items.length || 0;
  return {
    ok: checks.every((c) => c.ok),
    checks,
    brings: {
      summary_md: bringsText(card, d, upgrading ? version : null),
      commands: cnt('命令'), agents: cnt('助手角色'), skills: cnt('技能'),
      nodes: ins.map((x) => ({ id: x.id, title: x.title, placement: x.at })), pages, subpages,
    },
    ui_effect: uiEffect(card),
    effective: DATA_CATEGORIES.includes(card.category) ? 'immediate' : 'next_message',
    impact: (upgrading && d.upgrade_impact) || { needs_review_after_upgrade: 0, affected_artifacts: 0, note: null },
    blocked_reason: depOk ? null : '需要先升级基础建模能力',
  };
}

export function register(ctx) {
  const { on, json, err, db, sc, F, cardOf, forbid, uid } = ctx;
  const flags = pluginFlags(sc);
  db.plugins = { installed: new Set(installedProviders()) };
  db.packCatalog = () => catalogCards(flags);
  // 初始能力表按 mock 状态改：held 时场景模拟没排升级；ind 时零售连锁可更新到 1.1.0
  db.seedProjectPacks = (pid, packs) => packs.forEach((p) => {
    if (flags.has('held') && p.name === 'trace' && p.pending?.kind === 'upgrade') Object.assign(p, { status: 'enabled', pending: null });
    if (flags.has('ind') && p.name === 'industry-retail') p.latest_available = '1.1.0';
  });

  // 本项目的能力表（PL-05），带着原请求的令牌转调
  async function projectItems(pid, init) {
    const res = await ctx.self(`/api/v1/projects/${encodeURIComponent(pid)}/packs`, { headers: init?.headers });
    if (!res.ok) return { error: res };
    return { items: (await res.json()).items || [] };
  }

  // 目录（带不带项目视角）。不带项目时不列只对某些项目适用的租户包：新建向导（不带项目）沿用 packs() 的结果
  async function cardsFor(pid, init) {
    const all = [...catalogCards(flags), ...(db.privateCards || [])]; // 普通用户装的私有包（PL-26）
    if (!pid) {
      const o = { project: false, installed: db.plugins.installed, enabledNames: [] };
      return { cards: all.filter((c) => !APPLIES_TO[c.name]).map((c) => projectCard(c, null, o)), items: null };
    }
    if (!cardOf(pid)) return { error: err(404, 'project_not_found', '项目不存在') };
    const r = await projectItems(pid, init);
    if (r.error) return r;
    const o = { project: true, installed: db.plugins.installed, enabledNames: activeNames(r.items) };
    return { cards: all.map((c) => projectCard(c, r.items.find((p) => p.name === c.name), o)), items: r.items };
  }

  const knownVersion = (card, d, version) => version === card.latest || d.changelog.some((c) => c.version === version);

  // ---------- PL-27 开发 skill ----------
  on('GET', '/packs/dev-skills/([^/]+)', ({ m }) => {
    const category = decodeURIComponent(m[1]);
    if (!DEV_SKILL_CATEGORIES.includes(category)) return err(404, 'not_found', '这个分类没有开发 skill');
    return new Response(new Blob(['PK'.padEnd(22, ' ')]), {
      status: 200, headers: { 'Content-Type': 'application/zip', 'Content-Disposition': `attachment; filename="onto-plugin-dev-${category}.zip"` },
    });
  });

  // ---------- PL-01 插件目录 ----------
  on('GET', '/packs', async ({ query, init }) => {
    const pid = query.get('project_id') || '';
    const category = (query.get('category') || '').split(',').filter(Boolean);
    if (category.some((c) => !CATEGORIES.includes(c))) return err(400, 'invalid_argument', `category 只能取 ${CATEGORIES.join('|')}`, { fields: { category: 'enum' } });
    const r = await cardsFor(pid, init);
    if (r.error) return r.error;
    const items = filterCatalog(r.cards, {
      category, q: query.get('q') || '', installedOnly: query.get('installed_only') === 'true', updatableOnly: query.get('updatable_only') === 'true',
    });
    const body = { items, counts: catalogCounts(r.cards, { project: !!pid }) };
    if (pid) body.recommendations = flags.has('norec') || pid !== F.MAIN_PID ? [] : recommendationsOf(r.cards);
    return json(200, body);
  });

  // ---------- PL-02 插件详情 ----------
  const stats = () => {
    const by = {};
    ENTRIES.forEach((e) => { by[e.kind] = (by[e.kind] || 0) + 1; });
    return {
      entries: { total: ENTRIES.length, note: Object.entries(KIND_LABEL).filter(([k]) => by[k]).map(([k, t]) => `${t} ${by[k]}`).join(' · ') },
      knowledge: { project: projectEntries().length, industry: industryEntries().length },
      scripts: scripts().length,
      flowcharts: { total: FLOWCHARTS.length, note: `已确认 ${FLOWCHARTS.filter((f) => f.state === 'confirmed').length} · 草稿 ${FLOWCHARTS.filter((f) => f.state === 'draft').length}` },
      snapshots: seedSnapshots().length,
    };
  };
  on('GET', '/packs/([^/]+)', async ({ m, query, init }) => {
    const name = decodeURIComponent(m[1]);
    const pid = query.get('project_id') || '';
    const r = await cardsFor(pid, init);
    if (r.error) return r.error;
    const card = r.cards.find((c) => c.name === name);
    if (!card) return err(404, 'pack_not_found', `包 ${name} 不存在`);
    const d = detailOf(card, flags);
    const pp = r.items?.find((p) => p.name === name && p.status !== 'disabled');
    const version = query.get('version') || pp?.locked_version || card.latest;
    if (!knownVersion(card, d, version)) return err(404, 'pack_version_not_found', `包 ${name} 没有版本 ${version}`);
    const coreLocked = r.items?.find((p) => p.name === 'core')?.locked_version;
    const changelog = d.changelog.map((c) => {
      const range = requiresCore(name, c.version, flags);
      return {
        ...c,
        tags: [
          ...(pp?.locked_version === c.version ? ['in_use'] : []),
          ...(coreLocked && range && !satisfies(coreLocked, range) ? ['needs_core_upgrade'] : []),
        ],
      };
    });
    const provider = card.category === 'provider';
    const range = requiresCore(name, version, flags);
    const withPages = ['page', 'subpages'].includes(card.ui?.kind);
    return json(200, {
      ...card,
      version,
      description: d.description,
      size: d.size,
      files: d.files,
      released_at: d.changelog.find((c) => c.version === version)?.date || null,
      released_by: 'admin02',
      enabled_at: pp?.enabled_at || null,
      requires: { depends: range ? { core: range } : {}, kernel_api: '>=1.0 <2.0', page_api: withPages ? '>=1.0 <2.0' : null },
      flow_insertions: d.flow_insertions,
      flow_note: d.flow_note,
      project_stats: provider || (pid === F.MAIN_PID && isInstalledStatus(card.project_status)) ? projectStatsOf(name, stats()) : null,
      contents: d.contents,
      config: d.config,
      changelog,
      hot_level: provider ? 'H2' : DATA_CATEGORIES.includes(card.category) ? 'H0' : 'H1',
      effective: DATA_CATEGORIES.includes(card.category) ? 'immediate' : 'next_message',
    });
  });

  // ---------- PL-03 兼容检查 ----------
  on('GET', '/packs/([^/]+)/versions/([^/]+)/compatibility', async ({ m, query, init }) => {
    const [name, version] = [decodeURIComponent(m[1]), decodeURIComponent(m[2])];
    const pid = query.get('project_id');
    if (!pid) return err(400, 'invalid_argument', '缺少 project_id', { fields: { project_id: 'required' } });
    const r = await cardsFor(pid, init);
    if (r.error) return r.error;
    const card = r.cards.find((c) => c.name === name);
    if (!card) return err(404, 'pack_not_found', `包 ${name} 不存在`);
    const d = detailOf(card, flags);
    if (!knownVersion(card, d, version)) return err(404, 'pack_version_not_found', `包 ${name} 没有版本 ${version}`);
    return json(200, compatibilityOf(card, version, d, { items: r.items, flags }));
  });

  // ---------- PL-04 申请平台安装、PL-19 平台安装提供方 ----------
  const providerCheck = (name) => {
    const card = catalogCards(flags).find((c) => c.name === name);
    if (!card) return { error: err(404, 'pack_not_found', `包 ${name} 不存在`) };
    if (card.category !== 'provider') return { error: err(400, 'invalid_argument', '只有数据接入插件装在平台上', { fields: { name: 'not_provider' } }) };
    if (card.project_status === 'offline') return { error: err(409, 'pack_unavailable', '离线部署不可用', { reason: 'offline' }) };
    if (db.plugins.installed.has(name)) return { error: err(409, 'pack_already_enabled', '平台已经装了这个数据接入插件') };
    return { card };
  };
  on('POST', '/packs/([^/]+)/install-requests', ({ m, body, user }) => {
    const c = providerCheck(decodeURIComponent(m[1]));
    if (c.error) return c.error;
    // 进管理员审核申请（AD-08～AD-10，routes-pack-uploads.js；没挂界面），管理员收到 request_pending 通知
    const version = body.version || c.card.latest;
    const req = addAdminRequest(ctx, {
      kind: 'pack_install', requested_by: user?.username || null, project_id: null, summary: `${c.card.label} ${version}`,
      payload: { pack: { name: c.card.name, label: c.card.label }, version, note: body.note || null },
    });
    return json(202, { request_id: req.id });
  });
  on('POST', '/admin/packs/([^/]+)/install', ({ m, body, user }) => {
    const denied = forbid(user, ['admin']);
    if (denied) return denied;
    const c = providerCheck(decodeURIComponent(m[1]));
    if (c.error) return c.error;
    if (body.version && body.version !== c.card.latest) return err(404, 'pack_version_not_found', `包 ${c.card.name} 没有版本 ${body.version}`);
    db.plugins.installed.add(c.card.name); // 真实环境装进连接器网关要几分钟（O-40 任务）；mock 直接装好
    return json(202, { task_id: `task-${uid()}` });
  });
}
