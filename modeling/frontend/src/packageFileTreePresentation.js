import { directoryPathsForFile } from './packageFileTree.js';

const DIRECTORY_LABELS = {
  operation: '操作', object: '对象', diagram: '流程图',
  rule: '规则', metric: '指标', script: '脚本', mapping: '映射',
  asset: '资源', data: '数据', doc: '文档', resource: '资源',
  attachment: '附件', source: '来源', test: '测试', config: '配置',
  template: '模板', example: '示例', schema: '结构',
  runtime: '运行', log: '日志', modeling: '建模',
};

export function directoryLabel(name) {
  const value = String(name || '').toLowerCase();
  if (!/[a-z]/u.test(value) || /[\u3400-\u9fff]/u.test(value)) return '';
  return DIRECTORY_LABELS[value] || DIRECTORY_LABELS[value.replace(/s$/u, '')] || '目录';
}

function compareNodes(left, right) {
  if (left.type !== right.type) return left.type === 'directory' ? -1 : 1;
  return left.name.localeCompare(right.name, 'zh-CN', { numeric: true, sensitivity: 'base' });
}

/** 只调整展示层级；文件路径和目录标识保留，以兼容打开、保存、引用与展开状态。 */
export function presentPackageFileTree(tree) {
  const diagrams = tree.find((node) => node.type === 'directory' && node.path === 'diagrams');
  if (!diagrams) return tree;
  const operations = tree.find((node) => node.type === 'directory' && node.path === 'operations');
  const grouped = {
    ...operations,
    type: 'directory', name: 'operations', path: 'operations',
    children: [...(operations?.children || []), diagrams].sort(compareNodes),
    fileCount: (operations?.fileCount || 0) + diagrams.fileCount,
    modifiedCount: (operations?.modifiedCount || 0) + diagrams.modifiedCount,
  };
  return [...tree.filter((node) => node !== diagrams && node !== operations), grouped].sort(compareNodes);
}

export function displayDirectoryPathsForFile(path) {
  const ancestors = directoryPathsForFile(path);
  return ancestors[0] === 'diagrams' ? ['operations', ...ancestors] : ancestors;
}
