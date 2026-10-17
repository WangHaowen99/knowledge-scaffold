// 对话里「这一轮改了哪些文件」卡片的数据整理：按业务类别分组，并生成一句大白话标题。
// 纯函数，便于单测；展示组件见 ChatPane 的 ChangesCard。

// 目录 → 用户看得懂的类别名（与 USER_VOCABULARY 的术语约定一致），按出现顺序展示
const CATEGORIES = [
  ['research/', '资料分析报告'],
  ['ontology/object/', '业务对象'],
  ['ontology/operation/', '业务逻辑'],
  ['ontology/rule/', '规则'],
  ['ontology/metric/', '指标'],
  ['ontology/diagrams-summary/', '流程图摘要'],
  ['ontology/diagrams/', '流程图'],
  ['ontology/excel/', 'Excel 补充表'],
  ['ontology/data-mock/', '测试数据'],
  ['ontology/scripts/', '生成代码'],
  ['ontology/knowledge/', '业务知识'],
  ['reports/', '校验报告'],
  ['resource/', '资料'],
];
const OTHER = '其他文件';

const STATUS_VERB = { added: '新建', modified: '修改', deleted: '删除' };

export function categoryOf(path) {
  const hit = CATEGORIES.find(([prefix]) => path.startsWith(prefix));
  return hit ? hit[1] : OTHER;
}

// 去掉目录与扩展名，只留文件名本身（业务对象、逻辑等文档名就是业务名称）
export function displayName(path) {
  const base = path.split('/').pop() || path;
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(0, dot) : base;
}

// entries: [{ path, status: 'added' | 'modified' | 'deleted' }]
export function groupChanges(entries) {
  const order = [...CATEGORIES.map(([, label]) => label), OTHER];
  const groups = new Map();
  for (const entry of entries) {
    const label = categoryOf(entry.path);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push({ ...entry, name: displayName(entry.path) });
  }
  return order.filter((label) => groups.has(label)).map((label) => ({ label, items: groups.get(label) }));
}

// 「这一轮新建了 38 个文件」/「这一轮改动了 5 个文件：新建 2 个，修改 3 个」
export function changeHeadline({ added = 0, modified = 0, deleted = 0 }) {
  const counts = { added, modified, deleted };
  const parts = Object.entries(counts).filter(([, n]) => n > 0);
  const total = added + modified + deleted;
  if (parts.length === 1) {
    const [status, n] = parts[0];
    return `这一轮${STATUS_VERB[status]}了 ${n} 个文件`;
  }
  const detail = parts.map(([status, n]) => `${STATUS_VERB[status]} ${n} 个`).join('，');
  return `这一轮改动了 ${total} 个文件：${detail}`;
}

export const STATUS_LABELS = STATUS_VERB;
