import {
  ApartmentOutlined,
  AppstoreOutlined,
  ArrowLeftOutlined,
  BookOutlined,
  CheckOutlined,
  CopyOutlined,
  DatabaseOutlined,
  DeploymentUnitOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  HistoryOutlined,
  NodeIndexOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Dropdown,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import 'highlight.js/styles/atom-one-light.css';
// 暗色模式的代码高亮覆盖在 styles.css（[data-theme='dark'] .hljs-*）
import mermaid from 'mermaid';
import { Suspense, lazy, memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';
import { api } from '../lib/api.js';
import { SOLVER_DIR, isManagedSolverPath } from '../lib/solver-contract-model.js';
import CodeEditor from './cm/CodeEditor.jsx';
import MergeDiff from './cm/MergeDiff.jsx';
import ExcelView from './ExcelView.jsx';
import HtmlPreview from './HtmlPreview.jsx';

const StudioView = lazy(() => import('./StudioView.jsx'));
const SolverModelPanel = lazy(() => import('./SolverModelPanel.jsx'));
// modeling 同款流程图编辑器（ReactFlow）；drawio iframe 只在高级编辑时加载
const FlowchartView = lazy(() => import('./flowchart/FlowchartView.jsx'));

// mermaid 配色跟随应用主题（CSS 变量取值）；主题切换时重配并触发重渲染
let mermaidThemeVersion = 0;
function configureMermaid() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();
  const dark = document.documentElement.dataset.theme === 'dark';
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'base',
    themeVariables: {
      fontFamily: v('--font-body', 'system-ui, sans-serif'),
      primaryColor: v('--color-primary-soft', '#e8f1fd'),
      primaryTextColor: v('--text-primary', dark ? '#edf1f7' : '#111827'),
      primaryBorderColor: v('--color-primary', '#256fd0'),
      lineColor: v('--text-tertiary', '#6f7785'),
      background: v('--surface-card', dark ? '#151b24' : '#ffffff'),
      mainBkg: v('--color-primary-soft', '#e8f1fd'),
      clusterBkg: v('--surface-subtle', dark ? '#1a2130' : '#f3f5f8'),
      edgeLabelBackground: v('--surface-card', '#ffffff'),
    },
  });
  mermaidThemeVersion += 1;
}
configureMermaid();
window.addEventListener('onto-theme-changed', configureMermaid);

function MermaidBlock({ code }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, '');
  const [svg, setSvg] = useState('');
  const [failed, setFailed] = useState(false);
  const [themeVersion, setThemeVersion] = useState(mermaidThemeVersion);

  useEffect(() => {
    const onTheme = () => setThemeVersion(mermaidThemeVersion);
    window.addEventListener('onto-theme-changed', onTheme);
    return () => window.removeEventListener('onto-theme-changed', onTheme);
  }, []);

  useEffect(() => {
    let active = true;
    mermaid
      .render(`mmd-${id}-${themeVersion}`, code)
      .then((result) => active && setSvg(result.svg))
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, [code, id, themeVersion]);

  if (failed) {
    return (
      <pre>
        <code>{code}</code>
      </pre>
    );
  }
  // svg 来自 mermaid 对代码块的受控渲染，非用户任意 HTML
  return <div className="mermaid-figure" dangerouslySetInnerHTML={{ __html: svg }} />;
}

// 代码块卡片：语言角标 + 一键复制
function CodeBlock({ className, children }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(0);
  const lang = /language-([\w-]+)/.exec(className || '')?.[1] || '';
  const text = extractText(children);

  const copy = () => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(true);
      clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), 1600);
    });
  };

  return (
    <div className="code-card">
      <div className="code-card-bar">
        <span className="code-lang">{lang || 'text'}</span>
        <button type="button" className="code-copy" onClick={copy}>
          {copied ? <CheckOutlined /> : <CopyOutlined />} {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className={className}>{children}</pre>
    </div>
  );
}

function extractText(node) {
  if (node == null) return '';
  if (typeof node === 'string') return node;
  if (Array.isArray(node)) return node.map(extractText).join('');
  return extractText(node.props?.children);
}

// ---- 产物路径实体芯片：消息/报告里的路径文本可点击打开（识别优于回忆） ----

const PATH_RE = /(?:research|reports|ontology|knowledge|resource)\/[^\s`）)，。;；:"'<>|]+\.(?:md|mmd|drawio|xlsx|xlsm|html|py|csv)/g;
// 行内代码路径判定：常见知识包目录开头 + 已知扩展名
const INLINE_PATH_RE = /^(?:ontology|research|reports|resource)\/[^\s`]+\.(?:md|py|json|yaml|yml|csv|mmd|drawio|xlsx?|html|txt)$/i;

const EXT_ICON = {
  md: '📄', mmd: '🔀', drawio: '🔀', xlsx: '📊', xlsm: '📊',
  html: '📈', py: '🐍', csv: '📊',
};

export function FileChip({ path, exists = true, onOpen, status }) {
  const name = path.split('/').pop();
  const ext = (name.split('.').pop() || '').toLowerCase();
  return (
    <span
      className={`file-chip${exists ? '' : ' missing'}${status ? ` ${status}` : ''}`}
      title={path}
      role="button"
      tabIndex={0}
      onClick={(e) => {
        e.stopPropagation();
        if (exists) onOpen?.(path);
        else message.info('文件不存在（可能已改名或删除）');
      }}
      onKeyDown={(e) => e.key === 'Enter' && exists && onOpen?.(path)}
    >
      <span className="file-chip-icon">{EXT_ICON[ext] || '📄'}</span>
      {name}
    </span>
  );
}

// 在 markdown 源文本里把产物路径包成链接语法；跳过围栏代码块与行内代码
function linkifyPaths(md) {
  const fenceParts = md.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~)/);
  return fenceParts
    .map((part, i) => {
      if (i % 2 === 1) return part; // 围栏代码块原样
      return part
        .split(/(`[^`\n]*`)/)
        .map((seg, j) => {
          if (j % 2 === 1) return seg; // 行内代码原样
          return seg.replace(PATH_RE, (m, offset, whole) => {
            // 已在链接/wiki 语法里的不重复包装
            const before = whole.slice(Math.max(0, offset - 2), offset);
            if (before.includes('[') || before.includes('(')) return m;
            return `[${m}](#file:${encodeURIComponent(m)})`;
          });
        })
        .join('');
    })
    .join('');
}

// memo：流式期间每帧重渲染的开销主要在 markdown 解析，内容不变时跳过
export const Markdown = memo(function Markdown({ content, onWikiLink, onOpenFile, knownPaths }) {
  const prepared = linkifyPaths(
    content.replace(
      /\[\[([^\]]+)\]\]/g,
      (_, name) => `[${name}](#wiki:${encodeURIComponent(name)})`,
    ),
  );
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{
          a({ href, children }) {
            if (href && href.startsWith('#wiki:')) {
              const name = decodeURIComponent(href.slice('#wiki:'.length));
              return (
                <span className="wiki-link" onClick={() => onWikiLink?.(name)}>
                  {children}
                </span>
              );
            }
            if (href && href.startsWith('#file:')) {
              const path = decodeURIComponent(href.slice('#file:'.length));
              const exists = knownPaths ? knownPaths.has(path) : true;
              return <FileChip path={path} exists={exists} onOpen={onOpenFile} />;
            }
            return (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
          pre({ children }) {
            // mermaid 代码块由 code 渲染器整体接管，不包代码卡片
            const child = Array.isArray(children) ? children[0] : children;
            if (/language-mermaid/.test(child?.props?.className || '')) return children;
            return <CodeBlock className={child?.props?.className}>{children}</CodeBlock>;
          },
          code({ className, children, ...props }) {
            const text = String(children ?? '');
            if (/language-mermaid/.test(className || '')) {
              return <MermaidBlock code={text} />;
            }
            // 行内代码若是工作区文件路径 → 可点击芯片直接在中栏打开
            if (
              !className &&
              !text.includes('\n') &&
              INLINE_PATH_RE.test(text.trim()) &&
              (!knownPaths || knownPaths.has(text.trim()))
            ) {
              return <FileChip path={text.trim()} exists onOpen={onOpenFile} />;
            }
            return (
              <code className={className} {...props}>
                {children}
              </code>
            );
          },
        }}
      >
        {prepared}
      </ReactMarkdown>
    </div>
  );
});

const PREVIEWABLE = /\.(md|markdown|txt|py|json|yaml|yml|csv|mmd)$/i;

// 代码/配置文件只读预览：包成代码卡（语法高亮 + 复制），不再误当 markdown 渲染
function CodePreview({ content, lang }) {
  const longestFence = Math.max(2, ...[...content.matchAll(/`+/g)].map((m) => m[0].length));
  const fence = '`'.repeat(longestFence + 1);
  return <Markdown content={`${fence}${lang}\n${content}\n${fence}`} />;
}

// 简易 CSV 解析（支持双引号转义），复用 Excel 表格样式渲染
function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else cell += ch;
  }
  if (cell !== '' || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter((r) => r.length > 1 || (r[0] ?? '').trim() !== '');
}

function CsvTable({ text }) {
  const rows = useMemo(() => parseCsv(text).slice(0, 500), [text]);
  if (!rows.length) return <Empty description="空 CSV" />;
  return (
    <div className="excel-scroll">
      <table className="excel-table">
        <thead>
          <tr>
            {rows[0].map((cell, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <th key={i}>{cell}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(1).map((r, ri) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={ri}>
              {r.map((cell, ci) => (
                // eslint-disable-next-line react/no-array-index-key
                <td key={ci} className={cell !== '' && !Number.isNaN(Number(cell)) ? 'num' : undefined}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// 报告文件名 → 类型徽标与日期（{YYYYMMDD}-{HHMMSS}-{scope}-{type}.md 约定）
const REPORT_TYPES = [
  ['codegen-candidates', { label: '候选', color: 'purple' }],
  ['-rule', { label: '规则', color: 'blue' }],
  ['-semantic', { label: '语义', color: 'cyan' }],
  ['trace', { label: '追踪', color: 'green' }],
  ['testcase', { label: '追踪', color: 'green' }],
];

function reportMeta(path) {
  const name = path.split('/').pop().replace(/\.md$/i, '');
  const type = REPORT_TYPES.find(([hint]) => name.includes(hint))?.[1] || {
    label: '报告',
    color: 'default',
  };
  const match = name.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/);
  const time = match ? `${match[2]}-${match[3]} ${match[4]}:${match[5]}` : '';
  const title = match ? name.slice(16).replace(/^-/, '') : name;
  return { type, time, title: title || name };
}

// 中栏页签：滑动指示条 + 图标，替代默认 Segmented 的朴素样式
function CenterTabs({ value, onChange, items }) {
  const barRef = useRef(null);
  const [indicator, setIndicator] = useState(null);
  useLayoutEffect(() => {
    const active = barRef.current?.querySelector('[data-active="true"]');
    if (!active) {
      setIndicator(null);
      return;
    }
    const left = active.offsetLeft;
    const width = active.offsetWidth;
    setIndicator((prev) =>
      prev && prev.left === left && prev.width === width ? prev : { left, width },
    );
  });
  return (
    <div className="center-tabs" ref={barRef} role="tablist">
      {indicator && (
        <span
          className="center-tabs-thumb"
          style={{ transform: `translateX(${indicator.left}px)`, width: indicator.width }}
        />
      )}
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role="tab"
          aria-selected={value === item.value}
          data-active={value === item.value}
          className={`center-tab${value === item.value ? ' on' : ''}`}
          onClick={() => onChange(item.value)}
        >
          <span className="center-tab-icon">{item.icon}</span>
          <span className="center-tab-label">{item.label}</span>
        </button>
      ))}
    </div>
  );
}

// markdown 长文档目录：≥4 个标题时右侧浮动
function extractHeadings(md) {
  const headings = [];
  const clean = md.split(/```[\s\S]*?```/).join('\n');
  for (const match of clean.matchAll(/^(#{1,3})\s+(.+)$/gm)) {
    headings.push({ level: match[1].length, text: match[2].trim().slice(0, 40) });
  }
  return headings;
}

// 文件预览按类型分流：md 带目录、mmd 图、csv 表格、代码高亮、纯文本
function FilePreviewBody({ file, content, onWikiLink, onOpenFile, knownPaths }) {
  const ext = (file.split('.').pop() || '').toLowerCase();
  const containerRef = useRef(null);
  const isMd = ext === 'md' || ext === 'markdown';
  const headings = useMemo(() => (isMd ? extractHeadings(content) : []), [isMd, content]);

  const jumpTo = (text) => {
    const nodes = containerRef.current?.querySelectorAll('h1, h2, h3') || [];
    for (const node of nodes) {
      if (node.textContent.trim().startsWith(text.slice(0, 20))) {
        node.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
    }
  };

  if (ext === 'mmd') return <MermaidBlock code={content} />;
  if (ext === 'csv') return <CsvTable text={content} />;
  if (['py', 'json', 'yaml', 'yml'].includes(ext)) {
    return <CodePreview content={content} lang={ext === 'yml' ? 'yaml' : ext} />;
  }
  if (ext === 'txt') return <pre className="plain-text">{content}</pre>;
  return (
    <div className="md-with-toc">
      <div ref={containerRef} className="md-doc">
        <Markdown
          content={content}
          onWikiLink={onWikiLink}
          onOpenFile={onOpenFile}
          knownPaths={knownPaths}
        />
      </div>
      {headings.length >= 4 && (
        <nav className="md-toc" aria-label="文档目录">
          <div className="md-toc-title">目录</div>
          {headings.slice(0, 30).map((heading, index) => (
            <button
              // eslint-disable-next-line react/no-array-index-key
              key={index}
              type="button"
              className={`toc-item lv${heading.level}`}
              onClick={() => jumpTo(heading.text)}
            >
              {heading.text}
            </button>
          ))}
        </nav>
      )}
    </div>
  );
}

// codegen 候选评审：按候选分组勾选 + 参数预览，组装 /ontology-codegen 指令
function CodegenReview({ data, checked, onChecked, onGenerate }) {
  const toggle = (id) => {
    const next = new Set(checked);
    next.has(id) ? next.delete(id) : next.add(id);
    onChecked(next);
  };
  const selected = data.candidates.filter((c) => checked.has(c.id));
  if (!data.candidates.length && !data.unsuitable.length) {
    return <Empty description="报告里没有解析出候选（可切换查看原文）" />;
  }
  return (
    <div>
      {data.summary && (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
          {data.summary}
        </Typography.Paragraph>
      )}
      <Space style={{ marginBottom: 10 }} wrap>
        <Button
          size="small"
          onClick={() =>
            onChecked(new Set(data.candidates.filter((c) => c.verdict === '适合').map((c) => c.id)))
          }
        >
          全选 ✅ 适合
        </Button>
        <Button size="small" onClick={() => onChecked(new Set(data.candidates.map((c) => c.id)))}>
          全选
        </Button>
        <Button size="small" onClick={() => onChecked(new Set())}>
          清空
        </Button>
        <Button
          size="small"
          type="primary"
          disabled={selected.length === 0}
          onClick={() => onGenerate(selected)}
        >
          生成选中候选（{selected.length}）
        </Button>
      </Space>
      {data.candidates.map((c) => (
        <label key={c.id} className={`codegen-card${checked.has(c.id) ? ' checked' : ''}`}>
          <Checkbox checked={checked.has(c.id)} onChange={() => toggle(c.id)} />
          <div className="codegen-main">
            <div className="codegen-head">
              <span className="codegen-id">#{c.id}</span>
              <Typography.Text strong>{c.operation}</Typography.Text>
              {c.fragment && c.fragment !== '整段' && <Tag>{c.fragment}</Tag>}
              <Tag color={c.verdict === '适合' ? 'success' : 'warning'}>{c.verdict}</Tag>
              {c.type && <Tag color="blue">{c.type}</Tag>}
            </div>
            <div className="codegen-io">
              {c.inputs && c.inputs !== '—' && (
                <span>
                  输入 <code>{c.inputs}</code>
                </span>
              )}
              {c.outputs && c.outputs !== '—' && (
                <span>
                  输出 <code>{c.outputs}</code>
                </span>
              )}
            </div>
            {c.note && c.note !== '—' && <div className="codegen-note">{c.note}</div>}
          </div>
        </label>
      ))}
      {data.unsuitable.length > 0 && (
        <details className="codegen-fold">
          <summary>❌ 不适合（{data.unsuitable.length}）</summary>
          {data.unsuitable.map((u) => (
            <div key={u.operation} className="codegen-note">
              <b>{u.operation}</b>：{u.reason}
            </div>
          ))}
        </details>
      )}
      {data.generated.length > 0 && (
        <details className="codegen-fold">
          <summary>📦 已生成（{data.generated.length}）</summary>
          {data.generated.map((g) => (
            <div key={g.operation} className="codegen-note">
              <b>{g.operation}</b>：{g.scripts}
            </div>
          ))}
        </details>
      )}
    </div>
  );
}

// 报告评审：编号勾选 + 逐条确认（校验/追踪/codegen 候选通用）
function ReportReview({ data, checked, onChecked, onFixSelected }) {
  if (!data) return <Spin />;
  if (!data.items.length) {
    return <Empty description="没有解析出可评审条目（可切换查看报告原文）" />;
  }
  const toggle = (id) => {
    const next = new Set(checked);
    next.has(id) ? next.delete(id) : next.add(id);
    onChecked(next);
  };
  const groups = [];
  for (const item of data.items) {
    const last = groups[groups.length - 1];
    if (last && last.name === item.group) last.items.push(item);
    else groups.push({ name: item.group, items: [item] });
  }
  const selected = data.items.filter((i) => checked.has(i.id));
  return (
    <div>
      <Space style={{ marginBottom: 8 }} wrap>
        <Button
          size="small"
          onClick={() => onChecked(new Set(data.items.filter((i) => i.severity === 'error').map((i) => i.id)))}
        >
          全选错误项
        </Button>
        <Button size="small" onClick={() => onChecked(new Set(data.items.map((i) => i.id)))}>
          全选
        </Button>
        <Button size="small" onClick={() => onChecked(new Set())}>
          清空
        </Button>
        <Button
          size="small"
          type="primary"
          disabled={selected.length === 0}
          onClick={() => onFixSelected(selected)}
        >
          让助手修复选中项（{selected.length}）
        </Button>
      </Space>
      {groups.map((group) => (
        <div key={group.name || 'default'} className="review-group">
          {group.name && <div className="review-group-title">{group.name}</div>}
          {group.items.map((item) => (
            <label key={item.id} className={`review-item ${item.severity}`}>
              <Checkbox checked={checked.has(item.id)} onChange={() => toggle(item.id)} />
              <span className="review-id">#{item.id}</span>
              <Tag color={item.severity === 'error' ? 'error' : 'warning'} style={{ margin: 0 }}>
                {item.severity === 'error' ? '错误' : '警告'}
              </Tag>
              <span className="review-text">{item.text}</span>
            </label>
          ))}
        </div>
      ))}
    </div>
  );
}

function countByPrefix(paths, prefix) {
  // index.md 是目录索引，不算实体
  return paths.filter(
    (p) => p.startsWith(prefix) && p.endsWith('.md') && !p.endsWith('/index.md'),
  ).length;
}

function flattenPaths(nodes, out = []) {
  for (const node of nodes) {
    out.push(node.path);
    if (node.children) flattenPaths(node.children, out);
  }
  return out;
}

export default function CenterPane({
  project,
  tree,
  versions,
  usage,
  file,
  focus,
  onFocus,
  diffRev,
  onOpenFile,
  onCommand,
  canBack,
  onBack,
  onFocusPlain,
  queryPreset,
  diffFiles,
  onPickFile,
  recentFiles,
  drawioAvailable,
  onWorkspaceChanged,
}) {
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [conflict, setConflict] = useState(null); // {mine, theirs, revision}
  const [diffState, setDiffState] = useState(null); // {original, modified, rev}
  const [diffDraft, setDiffDraft] = useState('');
  const [pickRev, setPickRev] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [reportPath, setReportPath] = useState(null);
  const [reportContent, setReportContent] = useState(null);
  const [reportMode, setReportMode] = useState('review'); // review | raw
  const [reportItems, setReportItems] = useState(null);
  const [checkedItems, setCheckedItems] = useState(new Set());
  const [knowledge, setKnowledge] = useState(null);
  const [knowledgeDraft, setKnowledgeDraft] = useState(null); // {name, content}
  const [queryKind, setQueryKind] = useState('used_by');
  const [queryTerm, setQueryTerm] = useState('');
  const [queryResult, setQueryResult] = useState(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [studioVisited, setStudioVisited] = useState(false); // Studio 保持挂载不丢状态
  const [solverVisited, setSolverVisited] = useState(false); // 求解模型同样保持挂载，草稿不随切页丢失
  const [restoredDraft, setRestoredDraft] = useState(false); // 恢复了未保存草稿的提示
  const draftsRef = useRef(new Map()); // path -> 未保存草稿（跨文件切换保留，防丢稿）
  const rootRef = useRef(null);
  const topbarRef = useRef(null);

  // 页签栏吸顶：把它的实际高度交给正文里的二级吸顶元素（求解面板状态栏、文档目录）作偏移
  useLayoutEffect(() => {
    const root = rootRef.current;
    const bar = topbarRef.current;
    if (!root || !bar || typeof ResizeObserver === 'undefined') return undefined;
    const sync = () => root.style.setProperty('--center-bar-height', `${bar.offsetHeight}px`);
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(bar);
    return () => observer.disconnect();
  }, []);

  const paths = useMemo(() => flattenPaths(tree), [tree]);
  const knownPaths = useMemo(() => new Set(paths), [paths]);
  // 求解契约是可选能力：已有契约、或用户从进度/发布检查进入过时才露出页签
  const hasSolver = useMemo(() => paths.some((p) => p.startsWith(SOLVER_DIR)), [paths]);
  const reportPaths = useMemo(
    () => paths.filter((p) => p.startsWith('reports/') && p.endsWith('.md')).sort().reverse(),
    [paths],
  );

  useEffect(() => {
    if (focus === 'overview') {
      api.metrics(project.id).then(setCoverage).catch(() => {});
    }
  }, [focus, project.id, versions.length]);

  useEffect(() => {
    if (focus === 'studio') setStudioVisited(true);
    if (focus === 'solver') setSolverVisited(true);
  }, [focus]);

  useEffect(() => setSolverVisited(false), [project.id]);

  // 报告页默认选中最新一份（不再进门先见空态）
  useEffect(() => {
    if (focus === 'reports' && reportPaths.length && (!reportPath || !reportPaths.includes(reportPath))) {
      setReportPath(reportPaths[0]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, reportPaths]);

  // 有未保存草稿时拦截页面关闭
  useEffect(() => {
    const guard = (event) => {
      if (draftsRef.current.size > 0) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, []);

  useEffect(() => {
    if (focus !== 'reports' || !reportPath) {
      setReportContent(null);
      setReportItems(null);
      return;
    }
    setCheckedItems(new Set());
    api
      .readFile(project.id, reportPath)
      .then(setReportContent)
      .catch((error) => setReportContent({ error: error.message }));
    api
      .reportItems(project.id, reportPath)
      .then(setReportItems)
      .catch(() => setReportItems({ items: [] }));
  }, [focus, project.id, reportPath]);

  useEffect(() => {
    if (focus !== 'knowledge') return;
    api.listKnowledge(project.id).then((d) => setKnowledge(d.entries)).catch(() => {});
  }, [focus, project.id, versions.length]);

  // 文件加载（drawio/excel/html 有专属渲染组件，不走文本读取）
  const isDrawio = !!file && /\.(drawio|dio)$/i.test(file);
  const isExcel = !!file && /\.(xlsx|xlsm|xls)$/i.test(file);
  const isHtml = !!file && /\.html?$/i.test(file);
  useEffect(() => {
    setEditing(false);
    setConflict(null);
    if (!file || isDrawio || isExcel || isHtml) {
      setContent(null);
      return;
    }
    if (!PREVIEWABLE.test(file)) {
      setContent({ unsupported: true });
      return;
    }
    setLoading(true);
    setRestoredDraft(false);
    api
      .readFile(project.id, file)
      .then((data) => {
        setContent(data);
        // 该文件此前有未保存草稿 → 恢复编辑态（防丢稿：切换不丢，回来续编）
        const saved = draftsRef.current.get(file);
        if (saved != null && saved !== data.content) {
          setDraft(saved);
          setEditing(true);
          setRestoredDraft(true);
        } else {
          draftsRef.current.delete(file);
          setDraft(data.content);
        }
      })
      .catch((error) => setContent({ error: error.message }))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id, file]);

  const dirty = editing && content?.content != null && draft !== content.content;

  const changeDraft = (text) => {
    setDraft(text);
    if (content?.content != null && text !== content.content) draftsRef.current.set(file, text);
    else draftsRef.current.delete(file);
  };

  const cancelEditing = () => {
    if (dirty) {
      Modal.confirm({
        title: '放弃未保存的修改？',
        content: '取消编辑将丢弃本次改动（也可以先保存，或切去别处稍后回来继续）。',
        okText: '放弃修改',
        okButtonProps: { danger: true },
        cancelText: '继续编辑',
        onOk: () => {
          draftsRef.current.delete(file);
          setDraft(content.content);
          setEditing(false);
          setRestoredDraft(false);
        },
      });
      return;
    }
    setEditing(false);
    setRestoredDraft(false);
  };

  // 对比数据加载
  const rev = diffRev || pickRev;
  useEffect(() => {
    if (focus !== 'diff' || !file || !rev) {
      setDiffState(null);
      return;
    }
    Promise.all([
      api.fileAt(project.id, rev, file).catch(() => ({ content: '' })),
      api.readFile(project.id, file),
    ])
      .then(([old, current]) => {
        setDiffState({ original: old.content, modified: current.content, rev });
        setDiffDraft(current.content);
      })
      .catch((error) => message.error(error.message));
  }, [focus, project.id, file, rev]);

  const openWikiLink = (name) => {
    for (const dir of ['ontology/object', 'ontology/operation', 'ontology/rule', 'ontology/metric', 'ontology/logic']) {
      const candidate = `${dir}/${name}.md`;
      if (paths.includes(candidate)) {
        onOpenFile(candidate);
        return;
      }
    }
    message.info(`未找到 ${name} 对应的文档`);
  };

  const runQuery = async (kind = queryKind, term = queryTerm) => {
    if (['used_by', 'search', 'knowledge'].includes(kind) && !term.trim()) {
      message.warning('请填写查询词');
      return;
    }
    setQueryLoading(true);
    try {
      const data = await api.reverseQuery(project.id, kind, term.trim());
      setQueryResult(data.markdown);
    } catch (error) {
      message.error(error.message);
    } finally {
      setQueryLoading(false);
    }
  };

  // 左栏/树右键发起的反查：预置类型与词并立即执行
  useEffect(() => {
    if (!queryPreset) return;
    setQueryKind(queryPreset.kind);
    setQueryTerm(queryPreset.term || '');
    if (['used_by', 'search', 'knowledge'].includes(queryPreset.kind) && !queryPreset.term) return;
    runQuery(queryPreset.kind, queryPreset.term || '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryPreset?.nonce]);

  const saveDraft = async (text, after) => {
    try {
      const result = await api.writeFile(project.id, file, text, content?.revision || '');
      setContent((prev) => (prev ? { ...prev, revision: result.revision } : prev));
      message.success('已保存并记录版本');
      onWorkspaceChanged?.();
      after?.();
    } catch (error) {
      if (error.status === 409) {
        // 与助手写入撞车：拉最新内容给用户对比合并，不静默覆盖（方案 7.4）
        const latest = await api.readFile(project.id, file).catch(() => null);
        if (latest) setConflict({ mine: text, theirs: latest, after });
        return;
      }
      message.error(error.message);
    }
  };

  const resolveConflictSave = async (text) => {
    const { theirs, after } = conflict;
    setConflict(null);
    try {
      const result = await api.writeFile(project.id, file, text, theirs.revision);
      setContent({ ...theirs, content: text, revision: result.revision });
      setDraft(text);
      message.success('已保存并记录版本');
      onWorkspaceChanged?.();
      after?.();
    } catch (error) {
      message.error(error.message);
    }
  };

  return (
    <div ref={rootRef}>
      <div className="center-topbar" ref={topbarRef}>
        <Space align="center">
          {canBack && (
            <Button
              size="small"
              icon={<ArrowLeftOutlined />}
              onClick={onBack}
              title="返回上一个视图"
            />
          )}
          <CenterTabs
            value={focus}
            // 页签是平级切换（重置返回栈）；只有下钻动作（打开文件/对比/评审）才入栈
            // 对比/报告/反查暂不露出（保留内部路由，入口后续再开）
            onChange={(next) => (onFocusPlain || onFocus)(next)}
            items={[
              { label: '概览', value: 'overview', icon: <AppstoreOutlined /> },
              { label: 'Studio', value: 'studio', icon: <DeploymentUnitOutlined /> },
              {
                label: file
                  ? (() => {
                      const name = file.split('/').pop();
                      return name.length > 12 ? `${name.slice(0, 11)}…` : name;
                    })()
                  : '文件',
                value: 'file',
                icon: <FileTextOutlined />,
              },
              { label: '知识', value: 'knowledge', icon: <BookOutlined /> },
              ...(hasSolver || solverVisited || focus === 'solver'
                ? [{ label: '求解', value: 'solver', icon: <NodeIndexOutlined /> }]
                : []),
            ]}
          />
        </Space>
      </div>

      {studioVisited && (
        // 保持挂载：从条目跳文件再返回时不丢 Studio 的视图/选中状态
        <div
          className="center-anim"
          key="studio"
          style={{ display: focus === 'studio' ? undefined : 'none' }}
        >
          <Suspense fallback={<Spin style={{ marginTop: 48 }} />}>
            <StudioView
              project={project}
              onOpenFile={onOpenFile}
              onCommand={onCommand}
              onWorkspaceChanged={onWorkspaceChanged}
            />
          </Suspense>
        </div>
      )}

      {solverVisited && (
        <div
          className="center-anim"
          key="solver"
          style={{ display: focus === 'solver' ? undefined : 'none' }}
        >
          <Suspense fallback={<Spin style={{ marginTop: 48 }} />}>
            <SolverModelPanel
              key={project.id}
              project={project}
              versions={versions}
              onOpenFile={onOpenFile}
              onWorkspaceChanged={onWorkspaceChanged}
              onCommand={onCommand}
            />
          </Suspense>
        </div>
      )}

      {focus === 'overview' && (
        <div className="center-anim center-narrow" key="overview">
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <div className="stat-grid">
              <div className="stat-tile tone-blue">
                <DatabaseOutlined className="stat-icon" />
                <div className="stat-num">{countByPrefix(paths, 'ontology/object/')}</div>
                <div className="stat-label">业务对象</div>
              </div>
              <div className="stat-tile tone-violet">
                <ApartmentOutlined className="stat-icon" />
                <div className="stat-num">
                  {countByPrefix(paths, 'ontology/operation/') + countByPrefix(paths, 'ontology/logic/')}
                </div>
                <div className="stat-label">业务操作</div>
              </div>
              <div className="stat-tile tone-green">
                <div className="stat-num">{countByPrefix(paths, 'ontology/rule/')}</div>
                <div className="stat-label">规则</div>
              </div>
              <div className="stat-tile tone-amber">
                <div className="stat-num">{countByPrefix(paths, 'ontology/metric/')}</div>
                <div className="stat-label">指标</div>
              </div>
              <div className="stat-tile tone-teal">
                <FileSearchOutlined className="stat-icon" />
                <div className="stat-num">{countByPrefix(paths, 'research/')}</div>
                <div className="stat-label">资料分析报告</div>
              </div>
              <div className="stat-tile tone-amber">
                <HistoryOutlined className="stat-icon" />
                <div className="stat-num">{versions.length}</div>
                <div className="stat-label">版本数</div>
              </div>
            </div>
            {coverage && (coverage.design_coverage != null || coverage.pass_rate != null) && (
              <div className="stat-grid">
                <div className="stat-tile tone-green">
                  <div className="stat-num">
                    {coverage.design_coverage != null ? `${coverage.design_coverage}%` : '—'}
                  </div>
                  <div className="stat-label">设计覆盖率</div>
                </div>
                <div className="stat-tile tone-blue">
                  <div className="stat-num">
                    {coverage.pass_rate != null ? `${coverage.pass_rate}%` : '—'}
                  </div>
                  <div className="stat-label">推演通过率</div>
                </div>
                <div
                  className="stat-tile tone-neutral"
                  style={{ cursor: 'pointer' }}
                  onClick={() => {
                    setReportPath(coverage.source);
                    onFocus('reports');
                  }}
                >
                  <div className="stat-label" style={{ marginTop: 14 }}>
                    来源：{coverage.source?.replace('reports/', '')}
                  </div>
                </div>
              </div>
            )}
            {usage && usage.calls > 0 && (
              <Card size="small" title="模型用量">
                <Space size={24} wrap>
                  <span>调用 {usage.calls} 次</span>
                  <span>输出 {usage.output_tokens.toLocaleString()} tokens</span>
                </Space>
              </Card>
            )}
            <Card size="small" title="最近版本">
              {versions.slice(0, 3).map((v) => (
                <div
                  key={v.commit}
                  className="version-row"
                  role="button"
                  tabIndex={0}
                  title="点击进入对比视图"
                  onClick={() => {
                    setPickRev(v.commit);
                    onFocus('diff');
                  }}
                >
                  <Typography.Text type="secondary">版本 {v.version}</Typography.Text>{' '}
                  {v.message}
                </div>
              ))}
              {versions.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            </Card>
            <Typography.Paragraph type="secondary">
              从左栏「进度」开始建模；知识包文件点击后在「文件」中预览与编辑。
            </Typography.Paragraph>
          </Space>
        </div>
      )}

      {focus === 'file' && (
        <div className="center-anim" key="file">
        <Spin spinning={loading}>
          {!file && (
            <Empty description="从左栏「知识包」选择文件">
              {recentFiles?.length > 0 && (
                <div className="recent-files">
                  <div className="recent-files-label">最近打开</div>
                  {recentFiles.map((path) => (
                    <FileChip
                      key={path}
                      path={path}
                      exists={knownPaths.has(path)}
                      onOpen={onOpenFile}
                    />
                  ))}
                </div>
              )}
            </Empty>
          )}
          {isHtml && <HtmlPreview project={project} path={file} />}
          {isDrawio && (
            <Suspense fallback={<Spin style={{ marginTop: 48 }} />}>
              <FlowchartView
                project={project}
                path={file}
                drawioAvailable={drawioAvailable}
                onWorkspaceChanged={onWorkspaceChanged}
              />
            </Suspense>
          )}
          {isExcel && <ExcelView project={project} path={file} />}
          {file && content?.unsupported && !isHtml && !isDrawio && !isExcel && (
            <Empty description={`${file} 暂不支持预览（当前仅支持文本类文件）`} />
          )}
          {file && content?.error && <Empty description={content.error} />}
          {file && content?.content != null && (
            <>
              <Space style={{ marginBottom: 8 }} align="center" wrap>
                <span className="file-crumb" title={file}>
                  {file.split('/').map((seg, i, arr) =>
                    i < arr.length - 1 ? (
                      // eslint-disable-next-line react/no-array-index-key
                      <span key={i} className="fc-dir">
                        {seg}
                        <span className="fc-sep">/</span>
                      </span>
                    ) : (
                      // eslint-disable-next-line react/no-array-index-key
                      <span key={i} className="fc-name">
                        {seg}
                      </span>
                    ),
                  )}
                </span>
                {content.truncated && <Tag color="warning">仅显示前 512KB</Tag>}
                {dirty && <Tag color="warning">未保存</Tag>}
                {isManagedSolverPath(file) ? (
                  // 求解契约整目录受管（文件写接口拒绝），只能在面板里整体编辑与校验
                  <Button size="small" onClick={() => onFocus('solver')}>
                    在求解模型中编辑
                  </Button>
                ) : !editing ? (
                  <Button size="small" onClick={() => setEditing(true)}>
                    编辑
                  </Button>
                ) : (
                  <>
                    <Button
                      size="small"
                      type="primary"
                      disabled={!dirty}
                      onClick={() =>
                        saveDraft(draft, () => {
                          draftsRef.current.delete(file);
                          setContent({ ...content, content: draft });
                          setEditing(false);
                          setRestoredDraft(false);
                        })
                      }
                    >
                      保存
                    </Button>
                    <Button size="small" onClick={cancelEditing}>
                      取消
                    </Button>
                  </>
                )}
              </Space>
              {restoredDraft && (
                <Alert
                  type="info"
                  showIcon
                  closable
                  style={{ marginBottom: 8 }}
                  message="已恢复上次未保存的草稿"
                  action={
                    <Button size="small" onClick={cancelEditing}>
                      放弃草稿
                    </Button>
                  }
                />
              )}
              {editing ? (
                <CodeEditor key={file} value={draft} onChange={changeDraft} />
              ) : (
                <FilePreviewBody
                  file={file}
                  content={content.content}
                  onWikiLink={openWikiLink}
                  onOpenFile={onOpenFile}
                  knownPaths={knownPaths}
                />
              )}
            </>
          )}
        </Spin>
        </div>
      )}

      {focus === 'diff' && (
        <div className="center-anim" key="diff">
          {diffFiles?.length > 0 && (
            <div className="diff-files">
              <span className="diff-files-label">该轮变更：</span>
              {diffFiles.map((path) => (
                <FileChip
                  key={path}
                  path={path}
                  status={file === path ? 'modified' : undefined}
                  exists={knownPaths.has(path)}
                  onOpen={(p) => onPickFile?.(p)}
                />
              ))}
            </div>
          )}
          <Space style={{ marginBottom: 8 }} wrap>
            <Typography.Text>{file || '先从「知识包」或上方变更清单选择文件'}</Typography.Text>
            <Select
              size="small"
              style={{ minWidth: 220 }}
              placeholder="选择历史版本"
              value={rev}
              onChange={setPickRev}
              options={versions.map((v) => ({
                value: v.commit,
                label: `版本 ${v.version} · ${v.message.slice(0, 24)}`,
              }))}
            />
            {diffState && !isManagedSolverPath(file) && (
              <Button
                size="small"
                type="primary"
                onClick={() =>
                  saveDraft(diffDraft, () =>
                    setDiffState({ ...diffState, modified: diffDraft }),
                  )
                }
              >
                保存右侧内容
              </Button>
            )}
          </Space>
          {diffState ? (
            <MergeDiff
              key={`${file}-${diffState.rev}`}
              original={diffState.original}
              modified={diffState.modified}
              onModifiedChange={setDiffDraft}
            />
          ) : (
            <Empty description="选择文件与历史版本后展示对比（左：历史，右：当前可编辑）" />
          )}
        </div>
      )}

      {focus === 'reports' && (
        <div className="center-anim" key="reports">
          {reportPaths.length === 0 ? (
            <Empty description="还没有报告——运行校验或追踪后在这里评审" />
          ) : (
            <div className="report-layout">
              <div className="report-list">
                {reportPaths.map((path) => {
                  const meta = reportMeta(path);
                  return (
                    <div
                      key={path}
                      className={`report-item${reportPath === path ? ' active' : ''}`}
                      onClick={() => setReportPath(path)}
                      title={path}
                    >
                      <div className="report-item-head">
                        <Tag color={meta.type.color} style={{ margin: 0 }}>
                          {meta.type.label}
                        </Tag>
                        <span className="report-item-time">{meta.time}</span>
                      </div>
                      <div className="report-item-name">{meta.title}</div>
                    </div>
                  );
                })}
              </div>
              <div className="report-body">
                {!reportPath && <Empty description="选择左侧报告查看" />}
                {reportPath && reportContent?.error && <Empty description={reportContent.error} />}
                {reportPath && reportContent?.content != null && (
                  <>
                    <Space style={{ marginBottom: 8 }} wrap>
                      <Segmented
                        size="small"
                        value={reportMode}
                        onChange={setReportMode}
                        options={[
                          {
                            label:
                              reportItems?.type === 'codegen'
                                ? `候选评审（${reportItems.candidates.length}）`
                                : `逐条评审（${reportItems?.items?.length ?? 0}）`,
                            value: 'review',
                          },
                          { label: '报告原文', value: 'raw' },
                        ]}
                      />
                      <Button size="small" onClick={() => onCommand?.('/ontology-validate')}>
                        重新校验
                      </Button>
                      <Button size="small" onClick={() => onOpenFile(reportPath)}>
                        在编辑器中打开
                      </Button>
                    </Space>
                    {reportMode === 'raw' && (
                      <Markdown content={reportContent.content} onWikiLink={openWikiLink} onOpenFile={onOpenFile} knownPaths={knownPaths} />
                    )}
                    {reportMode === 'review' && reportItems?.type === 'codegen' && (
                      <CodegenReview
                        data={reportItems}
                        checked={checkedItems}
                        onChecked={setCheckedItems}
                        onGenerate={(selected) =>
                          onCommand?.(
                            '/ontology-codegen ' +
                              selected
                                .map((c) =>
                                  c.fragment && c.fragment !== '整段'
                                    ? `${c.operation}:${c.fragment}`
                                    : c.operation,
                                )
                                .join(' '),
                          )
                        }
                      />
                    )}
                    {reportMode === 'review' && reportItems?.type !== 'codegen' && (
                      <ReportReview
                        data={reportItems}
                        checked={checkedItems}
                        onChecked={setCheckedItems}
                        onFixSelected={(items) =>
                          onCommand?.(
                            `请修复报告 ${reportPath} 中的以下条目（引用原文）：\n` +
                              items.map((i) => `${i.id}. [${i.group}] ${i.text}`).join('\n') +
                              '\n逐条修复，完成后重新运行对应校验并汇报每条的处理结果。',
                          )
                        }
                      />
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {focus === 'knowledge' && (
        <div className="center-anim center-narrow" key="knowledge">
          <Space style={{ marginBottom: 8 }} wrap>
            <Typography.Title level={5} style={{ margin: 0 }}>
              业务知识库
            </Typography.Title>
            <Button
              size="small"
              type="primary"
              onClick={() => setKnowledgeDraft({ name: '', content: '## 背景\n\n## 规则\n\n' })}
            >
              新增知识条目
            </Button>
          </Space>
          {!knowledge && <Spin />}
          {knowledge && knowledge.length === 0 && (
            <Empty description="还没有沉淀的业务知识——对话中点助手消息旁的「沉淀知识」，或在此新增" />
          )}
          {knowledge?.map((entry) => (
            <div key={entry.path} className="knowledge-item msg-soft">
              <div className="knowledge-head">
                <Typography.Text strong style={{ cursor: 'pointer' }} onClick={() => onOpenFile(entry.path)}>
                  {entry.name}
                </Typography.Text>
                <span className="knowledge-time">
                  {new Date(entry.updated_at * 1000).toLocaleDateString()}
                </span>
                <Button size="small" type="text" onClick={() => onOpenFile(entry.path)}>
                  编辑
                </Button>
                <Popconfirm
                  title="删除该知识条目？"
                  onConfirm={async () => {
                    await api.deleteKnowledge(project.id, entry.path).catch((e) => message.error(e.message));
                    setKnowledge(knowledge.filter((k) => k.path !== entry.path));
                    onWorkspaceChanged?.();
                  }}
                >
                  <Button size="small" type="text" danger>
                    删除
                  </Button>
                </Popconfirm>
              </div>
              {entry.excerpt && <div className="knowledge-excerpt">{entry.excerpt}</div>}
            </div>
          ))}
          <Modal
            title="新增业务知识"
            open={!!knowledgeDraft}
            onCancel={() => setKnowledgeDraft(null)}
            okText="确认入库"
            onOk={async () => {
              if (!knowledgeDraft.name.trim()) {
                message.warning('请填写条目名称');
                return;
              }
              try {
                await api.createKnowledge(project.id, knowledgeDraft.name, knowledgeDraft.content);
                setKnowledgeDraft(null);
                api.listKnowledge(project.id).then((d) => setKnowledge(d.entries));
                onWorkspaceChanged?.();
                message.success('已入库并记录版本，后续建模会自动应用');
              } catch (error) {
                message.error(error.message);
              }
            }}
          >
            <Input
              placeholder="条目名称，如：退款只允许在支付后 24 小时内发起"
              value={knowledgeDraft?.name}
              onChange={(e) => setKnowledgeDraft({ ...knowledgeDraft, name: e.target.value })}
              style={{ marginBottom: 8 }}
            />
            <Input.TextArea
              autoSize={{ minRows: 6 }}
              value={knowledgeDraft?.content}
              onChange={(e) => setKnowledgeDraft({ ...knowledgeDraft, content: e.target.value })}
            />
          </Modal>
        </div>
      )}

      {focus === 'query' && (
        <div className="center-anim center-narrow" key="query">
          <Typography.Title level={5} style={{ marginTop: 0 }}>
            反查（确定性检索，不经模型）
          </Typography.Title>
          <Space style={{ marginBottom: 12 }} wrap>
            <Segmented
              size="small"
              value={queryKind}
              onChange={(kind) => {
                setQueryKind(kind);
                setQueryResult(null);
              }}
              options={[
                { label: '引用', value: 'used_by' },
                { label: '搜索', value: 'search' },
                { label: '孤立', value: 'orphans' },
                { label: '未用', value: 'unused' },
                { label: '失效链接', value: 'dangling' },
                { label: '知识', value: 'knowledge' },
              ]}
            />
            {['used_by', 'search', 'knowledge'].includes(queryKind) && (
              <Input
                size="small"
                style={{ width: 220 }}
                placeholder={queryKind === 'used_by' ? '元素名，如：订单' : '关键词'}
                value={queryTerm}
                onChange={(e) => setQueryTerm(e.target.value)}
                onPressEnter={() => runQuery()}
              />
            )}
            <Button size="small" type="primary" loading={queryLoading} onClick={() => runQuery()}>
              查询
            </Button>
          </Space>
          {queryResult && (
            <div className="query-result msg-soft">
              <Markdown content={queryResult} onWikiLink={openWikiLink} onOpenFile={onOpenFile} knownPaths={knownPaths} />
            </div>
          )}
          {!queryResult && !queryLoading && (
            <Empty description="选择反查类型后点击查询；产物树右键也可直达" />
          )}
        </div>
      )}

      <Modal
        title="文件已被他人或助手修改"
        open={!!conflict}
        width={860}
        onCancel={() => setConflict(null)}
        footer={
          conflict && [
            <Button key="theirs" onClick={() => resolveConflictSave(conflict.theirs.content)}>
              采用对方版本
            </Button>,
            <Button key="mine" type="primary" onClick={() => resolveConflictSave(conflict.mine)}>
              以我的内容保存
            </Button>,
          ]
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
          左侧是对方的最新版本，右侧是你的修改（可继续编辑后保存）；两个版本都会留在时间线里。
        </Typography.Paragraph>
        {conflict && (
          <MergeDiff
            original={conflict.theirs.content}
            modified={conflict.mine}
            onModifiedChange={(text) => setConflict({ ...conflict, mine: text })}
          />
        )}
      </Modal>
    </div>
  );
}
