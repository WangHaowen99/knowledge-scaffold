// 标准记录的组装（evidence-collection.md §1、§2）：锚点去重、同名加 ~n、关系去重、截断、哈希。
import { binAnchor, recordHash, relationAnchor } from './anchors.mjs';

export const RECORD_TYPES = ['entity', 'field', 'relation', 'operation', 'flow', 'rule', 'term'];
const TYPE_RANK = { entity: 0, operation: 1, field: 2, flow: 3, rule: 4, term: 5, relation: 9 };
const MAX_NAME_CHARS = 512;
const MAX_TEXT_CHARS = 2048;

/** attrs 里的字符串也来自目标程序：去掉孤立代理项（网关按 UTF-8 编码会失败）、截短，单条记录不会超过网关的 1 MiB 行上限。 */
function sanitize(value) {
  if (typeof value === 'string') {
    const text = value.toWellFormed();
    return text.length > MAX_TEXT_CHARS ? `${text.slice(0, MAX_TEXT_CHARS).toWellFormed()}…` : text;
  }
  // 记录里不放浮点数（两种语言写法不同，哈希会对不上）
  if (typeof value === 'number' && !Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return value.slice(0, 64).map(sanitize);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).slice(0, 64).map(([key, item]) => [sanitize(key), sanitize(item)]));
  }
  return value;
}

/** 程序里的名字原样保留，只把控制字符换成 ?、超长的截断，免得锚点和 JSONL 行失控。 */
export function cleanName(value) {
  const text = String(value ?? '').toWellFormed().replace(/[\u0000-\u001f\u007f]/g, '?');
  const chars = Array.from(text);
  return chars.length > MAX_NAME_CHARS ? `${chars.slice(0, MAX_NAME_CHARS).join('')}…` : text;
}

export class RecordSet {
  constructor({ source, provider }) {
    this.source = source;
    this.provider = provider;
    this.records = new Map();
    this.relationKeys = new Set();
  }

  has(anchor) {
    return this.records.has(anchor);
  }

  /**
   * 加一条非关系记录，返回最终锚点。
   * dedupe=true：同名视为同一个东西（外部接口、路由、IPC 通道），直接返回已有锚点；
   * 否则是程序里不同的东西撞名（重载、同名函数），第二个起加 ~2、~3。
   */
  add({ path, segments, type, parent = null, name, attrs = {}, method, confidence = 'observed', dedupe = false }) {
    const cleaned = segments.map(cleanName);
    const base = binAnchor(this.source, path, ...cleaned);
    let anchor = base;
    if (this.records.has(anchor)) {
      if (dedupe) return anchor;
      let suffix = 2;
      while (this.records.has(`${base}~${suffix}`)) suffix += 1;
      anchor = `${base}~${suffix}`;
    }
    this.records.set(anchor, {
      id: anchor,
      type,
      parent,
      name: cleanName(name),
      label: null,
      attrs: sanitize(attrs),
      evidence: { provider: this.provider, method, confidence },
    });
    return anchor;
  }

  /** 关系：同一 from/name/to 只留第一条；to 没有记录时传 null，端点放 attrs.url。 */
  relate({ from, name, to = null, attrs = {}, method, confidence = 'inferred' }) {
    if (typeof from !== 'string' || (to !== null && typeof to !== 'string') || from === to) return null;
    const target = to ?? attrs.url ?? '';
    const key = `${from}|${name}|${target}`;
    if (this.relationKeys.has(key)) return null;
    this.relationKeys.add(key);
    const anchor = relationAnchor(this.source, from, name, target);
    this.records.set(anchor, {
      id: anchor,
      type: 'relation',
      parent: null,
      name,
      label: null,
      from,
      to,
      attrs: sanitize(attrs),
      evidence: { provider: this.provider, method, confidence },
    });
    return anchor;
  }

  /**
   * 排好序、截断、算哈希。上级总排在下级前面（按层级深度），关系排最后，所以截断取前缀不会留下悬空的 parent；
   * 截断后两端不全的关系一并丢掉。
   */
  finish(maxRecords = Infinity) {
    const all = [...this.records.values()];
    const depthCache = new Map();
    const depth = (record) => {
      if (depthCache.has(record.id)) return depthCache.get(record.id);
      depthCache.set(record.id, 0);
      const parent = record.parent ? this.records.get(record.parent) : null;
      const value = parent ? depth(parent) + 1 : 0;
      depthCache.set(record.id, value);
      return value;
    };
    const order = new Map(all.map((record, index) => [record.id, index]));
    const rank = (record) => (record.type === 'relation' ? [99, 0] : [depth(record), TYPE_RANK[record.type] ?? 8]);
    all.sort((a, b) => {
      const [da, ta] = rank(a);
      const [db, tb] = rank(b);
      return da - db || ta - tb || order.get(a.id) - order.get(b.id);
    });
    let kept = all.slice(0, Math.max(0, maxRecords));
    const truncated = kept.length < all.length;
    if (truncated) {
      const ids = new Set(kept.filter((record) => record.type !== 'relation').map((record) => record.id));
      kept = kept.filter((record) => record.type !== 'relation'
        || (ids.has(record.from) && (record.to === null || ids.has(record.to))));
    }
    const counts = {};
    const records = kept.map((record) => {
      for (const key of ['parent', 'from', 'to']) {
        if (record[key] && !this.records.has(record[key])) record[key] = null;
      }
      counts[record.type] = (counts[record.type] ?? 0) + 1;
      return { ...record, hash: recordHash(record) };
    });
    return { records, counts, truncated, total: all.length };
  }
}
