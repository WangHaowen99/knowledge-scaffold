// Studio 表视图「最后更新时间」列的显示与排序；时间来自 /studio/updates（Unix 秒）。

const pad = (value) => String(value).padStart(2, '0');

export function formatUpdatedAt(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  const date = new Date(seconds * 1000);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

// antd Table 的 sorter：降序时会把返回值取反，取不到时间的行按方向反向处理，
// 保证升序、降序都排在最后
export function compareUpdatedAt(a, b, sortOrder) {
  const left = Number.isFinite(a.updatedAt) ? a.updatedAt : null;
  const right = Number.isFinite(b.updatedAt) ? b.updatedAt : null;
  if (left === null && right === null) return 0;
  const missingLast = sortOrder === 'descend' ? -1 : 1;
  if (left === null) return missingLast;
  if (right === null) return -missingLast;
  return left - right;
}

export function withUpdatedAt(nodes, updates) {
  return nodes.map((node) => ({ ...node, updatedAt: updates?.[node.path] }));
}
