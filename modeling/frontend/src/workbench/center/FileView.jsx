import { useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { parse as parseYaml } from 'yaml';
import { isPackageMarkdownHref, resolvePackageMarkdownTarget } from '../../packageMarkdownLink.js';
import { store } from '../lib/appStore.js';
import {
  displayEvidenceReferences,
  evidenceReferenceFromHref,
  linkEvidenceReferences,
} from '../lib/evidence.js';
import { useStore } from '../lib/store.js';
import { EvidenceLink } from './EvidenceLinks.jsx';

const SAMPLE = `## 生成采购建议

当可用量低于安全库存时，为该备件生成一条采购建议。

| 输入 | 说明 |
| --- | --- |
| 备件编码 | 必填 |
| 仓库 | 必填，决定安全库存取值 |

**步骤**

1. 查询可用库存（见 \`operations/查询可用库存.md\`）
2. 取该仓库级别的安全库存阈值
3. 低于阈值则按建议采购量公式生成一条建议

> 证据：material:采购流程说明.md#§2 · user-decision:d-014
`;

/* 这个组件是整个工作台唯一拖大依赖的地方，因此单独成块、按需加载。 */
export default function FileView({ path }) {
  const [mode, setMode] = useState('rendered');
  const demo = useStore(store, (state) => state.demo);
  const fileContents = useStore(store, (state) => state.fileContents);
  const materials = useStore(store, (state) => state.materials);
  const files = useStore(store, (state) => state.files);
  const content = demo ? SAMPLE : fileContents[path];
  const normalizedPath = String(path || '');
  const readableContent = linkEvidenceReferences(
    normalizedPath.endsWith('.md') ? stripFrontmatter(content) : content,
    demo ? [] : materials,
  );
  const readableSource = displayEvidenceReferences(content, demo ? [] : materials);
  const type = fileType(normalizedPath);
  const renderable = ['markdown', 'yaml', 'json'].includes(type);
  return (
    <div className={`wb-doc wb-doc-${type}`}>
      <div className="wb-file-toolbar">
        <span><code className="k">{normalizedPath}</code><small>{fileTypeLabel(type)}</small></span>
        {renderable && content !== undefined && (
          <div className="seg" role="tablist" aria-label="文件查看方式">
            <button role="tab" aria-selected={mode === 'rendered'} className={mode === 'rendered' ? 'on' : ''}
              onClick={() => setMode('rendered')}>渲染</button>
            <button role="tab" aria-selected={mode === 'source'} className={mode === 'source' ? 'on' : ''}
              onClick={() => setMode('source')}>源码</button>
          </div>
        )}
      </div>
      {content === undefined
        ? <div className="empty">正在读取文件…</div>
        : mode === 'rendered' && type === 'markdown'
          ? <article className="wb-markdown"><Markdown remarkPlugins={[remarkGfm]} components={{
              a: ({ node: _node, href, children, ...props }) => (
                <PackageLink path={normalizedPath} href={href} files={files} {...props}>{children}</PackageLink>
              ),
              img: ({ node: _node, alt }) => <span className="wb-markdown-image-note">图片：{alt || '未命名图片'}</span>,
            }}>{readableContent}</Markdown></article>
          : mode === 'rendered' && type === 'yaml'
            ? <StructuredView value={parseStructured(content, 'yaml', demo ? [] : materials)} format="yaml" />
            : mode === 'rendered' && type === 'json'
              ? <StructuredView value={parseStructured(content, 'json', demo ? [] : materials)} format="json" />
              : <pre className="wb-source">{readableSource}</pre>}
    </div>
  );
}

function fileType(path) {
  const extension = path.split('.').pop()?.toLowerCase();
  if (extension === 'md' || extension === 'markdown') return 'markdown';
  if (extension === 'yaml' || extension === 'yml') return 'yaml';
  if (extension === 'json') return 'json';
  if (extension === 'py') return 'python';
  return 'source';
}

function fileTypeLabel(type) {
  return ({ markdown: 'Markdown', yaml: 'YAML', json: 'JSON', python: 'Python', source: '文本' })[type];
}

function parseStructured(content, format, materials) {
  try {
    const data = format === 'yaml' ? parseYaml(String(content || '')) : JSON.parse(String(content || ''));
    return { data: displayStructuredReferences(data, materials) };
  } catch (error) {
    return { error: error instanceof Error ? error.message : '内容格式不正确' };
  }
}

function displayStructuredReferences(value, materials) {
  if (typeof value === 'string') return displayEvidenceReferences(value, materials);
  if (Array.isArray(value)) return value.map((item) => displayStructuredReferences(item, materials));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, displayStructuredReferences(item, materials)]));
  }
  return value;
}

const FIELD_LABELS = Object.freeze(Object.assign(Object.create(null), {
  id: '标识', name: '名称', title: '标题', version: '版本', status: '状态', description: '说明',
  resources: '知识内容', objects: '对象', operations: '操作', rules: '规则', metrics: '指标', scripts: '脚本',
  inputs: '输入', outputs: '输出', steps: '步骤', fields: '属性', type: '类型', required: '必填',
}));
const STATUS_LABELS = Object.freeze(Object.assign(Object.create(null), {
  released: '已发布', review: '待确认', draft: '草稿', deprecated: '已停用',
}));

function StructuredView({ value, format }) {
  if (value.error) {
    return <div className="wb-structured-error"><b>{format.toUpperCase()} 暂时无法渲染</b><span>{value.error}</span><small>可切换到源码查看并修正格式。</small></div>;
  }
  const data = value.data;
  const summary = data && typeof data === 'object' && !Array.isArray(data)
    ? [summaryScalar(data.name) || summaryScalar(data.title), data.version != null && summaryScalar(`v${String(data.version).replace(/^v/i, '')}`), statusLabel(data.status)].filter(Boolean)
    : [];
  return (
    <div className="wb-structured" aria-label={`${format.toUpperCase()} 结构化内容`}>
      {summary.length > 0 && <div className="wb-structured-summary">{summary.map((item, index) => <span key={`${index}-${item}`}>{item}</span>)}</div>}
      <StructuredNode value={data} depth={0} />
    </div>
  );
}

function StructuredNode({ name, value, depth }) {
  if (Array.isArray(value)) {
    return <StructuredGroup name={name} label="列表" items={value.map((item, index) => [`${index + 1}`, item])} depth={depth} />;
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value);
    if (name == null) return <StructuredChildren items={entries} depth={depth} />;
    return <StructuredGroup name={name} label="内容" items={entries} depth={depth} />;
  }
  return (
    <div className="wb-structured-row">
      <span>{fieldLabel(name, '')}</span>
      <Value value={value} />
    </div>
  );
}

function StructuredGroup({ name, label, items, depth }) {
  const [open, setOpen] = useState(depth < 1);
  return (
    <details className="wb-structured-group" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{fieldLabel(name, label)}<small>{items.length} 项</small></summary>
      {open && <StructuredChildren items={items} depth={depth + 1} />}
    </details>
  );
}

function StructuredChildren({ items, depth }) {
  const [limit, setLimit] = useState(100);
  const visible = items.slice(0, limit);
  return (
    <div className="wb-structured-children">
      {visible.length ? visible.map(([key, item], index) => (
        <StructuredNode key={`${index}-${key}`} name={key} value={item} depth={depth} />
      )) : <span className="wb-structured-empty">暂无内容</span>}
      {limit < items.length && <button className="btn btn-sm" onClick={() => setLimit((value) => value + 100)}>
        再显示 {Math.min(100, items.length - limit)} 项
      </button>}
    </div>
  );
}

function Value({ value }) {
  if (value == null) return <em>未设置</em>;
  if (typeof value === 'boolean') return <b className={`pill ${value ? 'pill-ok' : ''}`}>{value ? '是' : '否'}</b>;
  return <b>{String(value)}</b>;
}

function fieldLabel(name, fallback) {
  if (name == null) return fallback;
  return Object.hasOwn(FIELD_LABELS, name) ? FIELD_LABELS[name] : String(name).replaceAll('_', ' ');
}

function statusLabel(status) {
  if (!['string', 'number', 'boolean'].includes(typeof status)) return undefined;
  return Object.hasOwn(STATUS_LABELS, status) ? STATUS_LABELS[status] : String(status);
}

function summaryScalar(value) {
  return ['string', 'number', 'boolean'].includes(typeof value) ? String(value) : undefined;
}

function stripFrontmatter(markdown) {
  return String(markdown || '').replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '');
}

function PackageLink({ path, href, files, children, ...props }) {
  const evidenceReference = evidenceReferenceFromHref(href);
  if (evidenceReference) {
    return <EvidenceLink reference={evidenceReference} materials={store.get().materials}>{children}</EvidenceLink>;
  }
  if (isPackageMarkdownHref(href)) {
    return <a href={href} {...props} onClick={(event) => {
      event.preventDefault();
      const target = resolvePackageMarkdownTarget(path, href, files);
      if (target) actions.selectPath(target);
    }}>{children}</a>;
  }
  if (String(href || '').startsWith('#')) return <a href={href} {...props}>{children}</a>;
  return <a href={href} target="_blank" rel="noreferrer noopener" {...props}>{children}</a>;
}
