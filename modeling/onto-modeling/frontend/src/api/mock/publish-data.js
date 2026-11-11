// mock · 发布（PB-01～PB-07）的数据：门禁链、数据绑定问题、发布记录、包清单（合成场景）
import { MAIN_PID } from './fixtures.js';
import { entryRef } from './scenario.js';
import { addedAt, genericHistory, hex8, mainHistory } from './versions-data.js';

export const PACKAGE_TABLES = [
  'erp.replenish_order', 'erp.store', 'erp.store_request', 'erp.replenish_ledger', 'erp.goods_category',
  'erp.replenish_std', 'erp.std_catalog', 'erp.delivery_cycle', 'erp.expiry_std', 'erp.check_item',
];
// 没标「数据 Schema」的表格资料（PB-01 binding.table_resources）
export const UNMARKED_TABLE_RESOURCES = ['resource/others/08-门店陈列照片清单.xlsx'];
export const OBJECT_COUNT = 15;

// 数据绑定检查的三个问题（设计稿 publish 那三行，换成补货场景）
export function bindingIssues() {
  return [
    {
      object: entryRef('replenish_ledger'), declared: 'replenish_ledger', kind: 'declared_missing',
      message: 'Schema 资料里没有这张表', detail: null,
      candidates: [{ table: 'erp.replenish_ledger', matched: 2 }], fixes: ['set_table', 'set_none'], blocking: true,
    },
    {
      object: entryRef('store_request'), declared: 'crm.store_request', kind: 'no_columns',
      message: '属性英文名 category_code、max_qty；表列 item_code、item_val',
      detail: { attr_columns: ['category_code', 'max_qty'], table_columns: ['item_code', 'item_val'] },
      candidates: [], fixes: ['set_table', 'set_none'], blocking: true,
    },
    {
      object: entryRef('overage_list'), declared: 'overage_list', kind: 'declared_unverifiable',
      message: '没有标为「数据 Schema」的表格资料', detail: null,
      candidates: [], fixes: ['set_none', 'mark_schema_material'], blocking: true,
    },
  ];
}

const sha = (s) => `${hex8(s)}${hex8(`${s}#1`)}${hex8(`${s}#2`)}${hex8(`${s}#3`)}`.repeat(2);
const SOLVER_FROM = 12; // 求解契约从 v0.12 起启用：之前的版本按普通知识包发布

export const formatOf = (pid, n) => (pid === MAIN_PID && n >= SOLVER_FROM
  ? { format: 'solver', format_label: '求解知识包' } : { format: 'ontology', format_label: '普通知识包' });

export function releaseRecord(pid, v, { at, by = 'zhangsan', degraded = false, unbound = [], pulls = [], feedback = 0 } = {}) {
  const version = v.version_label.replace(/^v/, '').split('.').concat('0').slice(0, 3).join('.');
  const base = `/api/v1/projects/${encodeURIComponent(pid)}/releases/${version}`;
  return {
    version, version_label: v.version_label, commit: v.commit, tag: `release/${version}`, at: at || v.at, by,
    ...formatOf(pid, v.n),
    gates: { all_passed: !degraded, degraded, unbound_objects: unbound },
    manifest_attached: true, pulls, feedback_count: feedback,
    size: 180000 + v.n * 7300, sha256: sha(`${pid}:${version}`),
    project_id: pid, manifest_url: `${base}/manifest`, download_url: `${base}/download`,
  };
}

// 已有的发布记录（新的在前）；按项目在 db 里只建一次
export function releasesOf(db, pid, card) {
  db.releases = db.releases || new Map();
  if (db.releases.has(pid)) return db.releases.get(pid);
  let list = [];
  const shift = (iso, min) => new Date(Date.parse(iso) + min * 60000).toISOString();
  if (pid === MAIN_PID) {
    const h = mainHistory();
    const v7 = h.find((v) => v.n === 7);
    const v11 = h.find((v) => v.n === 11);
    list = [
      releaseRecord(pid, v11, {
        at: shift(v11.at, 15), degraded: true, unbound: ['超量清单', '审批提示'],
        pulls: [{ at: shift(v11.at, 42), by_token: '评测服务', variant: 'standard' }],
      }),
      releaseRecord(pid, v7, {
        at: shift(v7.at, 22),
        pulls: [{ at: shift(v7.at, 50), by_token: '评测服务', variant: 'standard' }, { at: shift(v7.at, 51), by_token: '评测服务', variant: 'graph' }],
        feedback: 1,
      }),
    ];
  } else if (card?.last_release) {
    const h = genericHistory(card);
    const v = h.find((x) => x.version_label === card.last_release.version) || h[h.length - 1];
    list = [releaseRecord(pid, v, { at: card.last_release.at, pulls: [{ at: card.last_release.at, by_token: '评测服务', variant: 'standard' }] })];
  }
  db.releases.set(pid, list);
  return list;
}

// PB-06 包清单：B2 第 2 条的实际形状（files 只有 path、size）
export function manifestOf(pid, rec, history, packageName) {
  const v = history.find((x) => x.commit === rec.commit);
  const added = addedAt(history);
  const n = v?.n ?? 0;
  const files = pid === MAIN_PID
    ? (history.length ? mainEntriesAt(n, added) : []).map((p, i) => ({ path: p, size: 420 + ((i * 137) % 900) }))
    : [];
  files.unshift({ path: 'manifest.yaml', size: 1260 }, { path: 'README.md', size: 2048 });
  const solver = rec.format === 'solver' ? { revision: 'r7' } : null;
  const degraded = rec.gates.degraded ? { unbound_objects: rec.gates.unbound_objects } : null;
  const packs = [{ name: 'core', version: '1.4.2' }, { name: 'industry-retail', version: n >= 11 || pid !== MAIN_PID ? '1.0.3' : '1.0.2' }];
  const yaml = [
    `package_name: ${packageName}`, `version: ${rec.version}`, `schema_version: "${rec.format === 'solver' ? '0.2' : '0.1'}"`,
    `source_commit: ${rec.commit}`, `released_at: ${rec.at}`, 'packs:', ...packs.map((p) => `  - ${p.name}@${p.version}`),
    ...(degraded ? ['degraded:', '  unbound_objects:', ...degraded.unbound_objects.map((o) => `    - ${o}`)] : []),
  ].join('\n');
  return {
    package_name: packageName, version: rec.version, schema_version: rec.format === 'solver' ? '0.2' : '0.1',
    source_commit: rec.commit, released_at: rec.at, packs, flow_hash: 'b41e09c2a7', solver, degraded, files,
    format: rec.format, sha256: rec.sha256, size: rec.size, manifest: yaml, truncated: false,
  };
}

function mainEntriesAt(n, added) {
  return [...ENTRY_PATHS].filter(([id]) => !added.has(id) || added.get(id) <= n).map(([, p]) => p);
}

const ENTRY_PATHS = new Map([
  'replenish_order', 'store', 'store_request', 'replenish_ledger', 'goods_category', 'safety_stock_std', 'applicable_std', 'display_qty_std',
  'delivery_cycle', 'expiry_loss_std', 'check_item', 'overage_list', 'approval_hint', 'intake_checks', 'safety_stock_calc', 'display_qty_check',
  'expiry_check', 'overage_and_hint', 'r05_by_category', 'r07_display_max', 'r09_store_stricter', 'r12_region_quota', 'build_replenish_order',
].map((k) => {
  const r = entryRef(k);
  return [r.id, r.kind === 'script' ? `scripts/${r.name}` : `ontology/${r.kind}/${r.name}.md`];
}));
