// mock · 3.20 知识包版本对比（PC-01～PC-08，参数与 develop 一致）与 HB-05：任意两侧都给同一份合成差异（新增、删除、修改、
// 未变化各有），发起后约 0.8 秒出结果。?mock=compare:cross 时上传的包算另一个知识包，没确认跨包就发起会失败。
const OLD = {
  'manifest.yaml': 'kind: ontology-package\nschema_version: \'0.1\'\nid: retail-restock\nversion: 0.12.0\nname: 门店补货\n',
  'objects/门店.md': '# 门店\n\n## 属性\n\n| 名称 | 英文名 | 类型 |\n|---|---|---|\n| 门店编码 | store_code | 文本 |\n| 面积 | area | 数值 |\n\n按区域分组管理。\n',
  'rules/限购规则.md': '# 限购规则\n\n单店单日补货不超过安全库存的两倍。\n',
  'metrics/周转天数.md': '# 周转天数\n\n库存 ÷ 日均销量。\n',
  'operations/生成补货单.md': '# 生成补货单\n\n按[[门店]]汇总缺口生成[[补货单]]。\n',
  'indexes/embedding.bin': null,
};
const NEW = {
  ...OLD,
  'manifest.yaml': OLD['manifest.yaml'].replace('0.12.0', '0.13.0'),
  'objects/门店.md': OLD['objects/门店.md'].replace('| 面积 | area | 数值 |\n', '| 面积 | area | 数值 |\n| 等级 | store_grade | 枚举 |\n').replace('按区域分组管理。', '按区域与等级分组管理；等级缺失时按 C 级处理。'),
  'objects/补货单.md': '# 补货单\n\n## 属性\n\n| 名称 | 英文名 | 类型 |\n|---|---|---|\n| 单号 | order_no | 文本 |\n| 门店 | store_code | 文本 |\n',
};
delete NEW['rules/限购规则.md'];

const CATEGORY = (path) => (path === 'manifest.yaml' ? 'metadata' : path.split('/')[0]);

function rows() {
  return [...new Set([...Object.keys(OLD), ...Object.keys(NEW)])].sort().map((path) => {
    const [a, b] = [OLD[path], NEW[path]];
    const side = (text) => (text === undefined ? null : { path, size: text === null ? 4096 : new Blob([text]).size, category: CATEGORY(path) });
    const status = a === undefined ? 'added' : b === undefined ? 'deleted' : a === b ? 'unchanged' : 'modified';
    return { path, category: CATEGORY(path), status, display_name: path.split('/').pop(), left: side(a), right: side(b) };
  });
}

function summary(list) {
  const files = { added: 0, deleted: 0, modified: 0, unchanged: 0 };
  const categories = {};
  list.forEach((r) => {
    files[r.status] += 1;
    categories[r.category] ||= { added: 0, deleted: 0, modified: 0, unchanged: 0 };
    categories[r.category][r.status] += 1;
  });
  const affected = Object.keys(categories).filter((k) => categories[k].added || categories[k].deleted || categories[k].modified);
  const evidence = list.filter((r) => r.status !== 'unchanged').slice(0, 4).map((r) => ({ path: r.path, display_name: r.display_name, status: r.status }));
  return {
    files, categories, affected_categories: affected, evidence,
    left_file_count: Object.keys(OLD).length, right_file_count: Object.keys(NEW).length,
  };
}

export function register({ on, json, err, sc, uid }) {
  const comparisons = new Map();
  const inputs = new Map();
  const base = '/projects/([^/]+)/package-comparisons';
  const ONTOLOGY = 'retail-restock-hub';

  on('POST', `${base}/inputs`, ({ form }) => {
    const file = form?.get('file');
    if (!file) return err(400, 'invalid_argument', '缺少文件', { fields: { file: 'required' } });
    const id = `cmpin_${uid()}`;
    const now = Date.now() / 1000;
    const item = {
      input_id: id, package_id: sc.compare === 'cross' ? 'another-package' : ONTOLOGY, package_name: '门店补货',
      package_version: '0.13.0', package_digest: 'f'.repeat(64), content_digest: 'f'.repeat(64),
      file_count: Object.keys(NEW).length, uncompressed_size: 8192, created_at: now, expires_at: now + 86400, warnings: [],
    };
    inputs.set(id, item);
    return json(201, item);
  });
  on('DELETE', `${base}/uploads/([^/]+)`, () => json(200, { cancelled: true }));
  on('DELETE', `${base}/inputs/([^/]+)`, ({ m }) => {
    if ([...comparisons.values()].some((c) => [c.left, c.right].some((s) => s.input_id === m[2]))) {
      return err(409, 'compare_input_in_use', '比较输入仍被任务使用，请先删除比较任务');
    }
    inputs.delete(m[2]);
    return json(200, { deleted: true });
  });
  on('POST', base, ({ body }) => {
    const id = `cmp_${uid()}`;
    const packageOf = (s) => (s.kind === 'upload' ? inputs.get(s.input_id)?.package_id : s.ontology_id);
    const cross = packageOf(body.left) !== packageOf(body.right);
    comparisons.set(id, { ...body, at: Date.now(), failed: cross && !body.allow_cross_package });
    return json(202, { comparison_id: id, status: 'queued', phase: 'queued', poll_after_ms: 500 });
  });
  const ready = (id) => {
    const c = comparisons.get(id);
    if (!c) return { gone: err(410, 'compare_expired', '比较结果已损坏或已过期') };
    if (Date.now() - c.at < 800) return { pending: { comparison_id: id, status: 'running', phase: 'reading' } };
    if (c.failed) return { pending: { comparison_id: id, status: 'failed', phase: 'failed', error: '两侧属于不同 Hub 知识包，请确认跨包对比' } };
    return { c };
  };
  const sideView = (s) => (s.kind === 'upload'
    ? { source: s, package_name: '门店补货', package_version: '0.13.0', package_digest: 'f'.repeat(64), file_count: Object.keys(NEW).length, uncompressed_size: 8192, version: '' }
    : { source: s, package_name: '门店补货', package_version: s.release_id, package_digest: 'e'.repeat(64), file_count: Object.keys(OLD).length, uncompressed_size: 8192, version: s.version || s.release_id.split('-').pop() }); // HB-05 由发布路由的 mock 给出
  on('GET', `${base}/([^/]+)`, ({ m }) => {
    const { gone, pending, c } = ready(m[2]);
    if (gone || pending) return gone || json(200, pending);
    return json(200, {
      comparison_id: m[2], status: 'ready', created_at: c.at / 1000, summary: summary(rows()),
      left: sideView(c.left), right: sideView(c.right),
    });
  });
  on('GET', `${base}/([^/]+)/files`, ({ m, query }) => {
    const { gone, pending } = ready(m[2]);
    if (gone) return gone;
    if (pending) return err(409, 'compare_not_ready', '比较尚未完成');
    const status = query.get('status') || '';
    const category = query.get('category') || '';
    const q = (query.get('q') || '').toLowerCase();
    const offset = Number(query.get('offset') || 0);
    const limit = Number(query.get('limit') || 50);
    const all = rows();
    const matched = all.filter((r) => (!status || (status === 'changed' ? r.status !== 'unchanged' : r.status === status))
      && (!category || r.category === category) && (!q || r.path.toLowerCase().includes(q) || r.display_name.toLowerCase().includes(q)));
    return json(200, { items: matched.slice(offset, offset + limit), total: matched.length, filtered_total: matched.length, overall_total: all.length, offset, limit });
  });
  on('GET', `${base}/([^/]+)/file-diff`, ({ query }) => {
    const path = query.get('file_id');
    const row = rows().find((r) => r.path === path);
    if (!row) return err(404, 'not_found', '比较文件不存在');
    const text = (value) => (value === undefined ? null : value === null
      ? { text: null, encoding: 'binary', bytes: 4096 } : { text: value, encoding: 'utf-8', bytes: new Blob([value]).size });
    return json(200, { path, status: row.status, category: row.category, left: text(OLD[path]), right: text(NEW[path]) });
  });
  on('DELETE', `${base}/([^/]+)`, ({ m }) => {
    comparisons.delete(m[2]);
    return json(200, { deleted: true });
  });
}
