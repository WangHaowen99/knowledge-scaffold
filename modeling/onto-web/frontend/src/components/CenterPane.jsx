import {
  ApartmentOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  HistoryOutlined,
} from '@ant-design/icons';
import {
  Button,
  Card,
  Empty,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import mermaid from 'mermaid';
import { Suspense, lazy, memo, useEffect, useId, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../lib/api.js';
import CodeEditor from './cm/CodeEditor.jsx';
import MergeDiff from './cm/MergeDiff.jsx';

const StudioView = lazy(() => import('./StudioView.jsx'));

mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });

function MermaidBlock({ code }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, '');
  const [svg, setSvg] = useState('');
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    mermaid
      .render(`mmd-${id}`, code)
      .then((result) => active && setSvg(result.svg))
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, [code, id]);

  if (failed) {
    return (
      <pre>
        <code>{code}</code>
      </pre>
    );
  }
  // svg 来自 mermaid 对代码块的受控渲染，非用户任意 HTML
  return <div dangerouslySetInnerHTML={{ __html: svg }} />;
}

// memo：流式期间每帧重渲染的开销主要在 markdown 解析，内容不变时跳过
export const Markdown = memo(function Markdown({ content, onWikiLink }) {
  const prepared = content.replace(
    /\[\[([^\]]+)\]\]/g,
    (_, name) => `[${name}](#wiki:${encodeURIComponent(name)})`,
  );
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
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
            return (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
          code({ className, children, ...props }) {
            const text = String(children ?? '');
            if (/language-mermaid/.test(className || '')) {
              return <MermaidBlock code={text} />;
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

function countByPrefix(paths, prefix) {
  return paths.filter((p) => p.startsWith(prefix) && p.endsWith('.md')).length;
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
}) {
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [diffState, setDiffState] = useState(null); // {original, modified, rev}
  const [diffDraft, setDiffDraft] = useState('');
  const [pickRev, setPickRev] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [reportPath, setReportPath] = useState(null);
  const [reportContent, setReportContent] = useState(null);

  const paths = useMemo(() => flattenPaths(tree), [tree]);
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
    if (focus !== 'reports' || !reportPath) {
      setReportContent(null);
      return;
    }
    api
      .readFile(project.id, reportPath)
      .then(setReportContent)
      .catch((error) => setReportContent({ error: error.message }));
  }, [focus, project.id, reportPath]);

  // 文件加载
  useEffect(() => {
    setEditing(false);
    if (!file) {
      setContent(null);
      return;
    }
    if (!PREVIEWABLE.test(file)) {
      setContent({ unsupported: true });
      return;
    }
    setLoading(true);
    api
      .readFile(project.id, file)
      .then((data) => {
        setContent(data);
        setDraft(data.content);
      })
      .catch((error) => setContent({ error: error.message }))
      .finally(() => setLoading(false));
  }, [project.id, file]);

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
    for (const dir of ['ontology/object', 'ontology/logic']) {
      const candidate = `${dir}/${name}.md`;
      if (paths.includes(candidate)) {
        onOpenFile(candidate);
        return;
      }
    }
    message.info(`未找到 ${name} 对应的文档`);
  };

  const saveDraft = async (text, after) => {
    try {
      await api.writeFile(project.id, file, text);
      message.success('已保存');
      after?.();
    } catch (error) {
      message.error(error.message);
    }
  };

  return (
    <div>
      <Segmented
        value={focus}
        onChange={onFocus}
        options={[
          { label: '概览', value: 'overview' },
          { label: 'Studio', value: 'studio' },
          { label: '文件', value: 'file' },
          { label: '对比', value: 'diff' },
          { label: '报告', value: 'reports' },
        ]}
        style={{ marginBottom: 12 }}
      />

      {focus === 'studio' && (
        <div className="center-anim" key="studio">
          <Suspense fallback={<Spin style={{ marginTop: 48 }} />}>
            <StudioView project={project} onOpenFile={onOpenFile} />
          </Suspense>
        </div>
      )}

      {focus === 'overview' && (
        <div className="center-anim" key="overview">
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <div className="stat-grid">
              <div className="stat-tile" style={{ '--tile': '#256fd0', '--tile-bg': '#e8f1fd' }}>
                <DatabaseOutlined className="stat-icon" />
                <div className="stat-num">{countByPrefix(paths, 'ontology/object/')}</div>
                <div className="stat-label">业务对象</div>
              </div>
              <div className="stat-tile" style={{ '--tile': '#7048c8', '--tile-bg': '#eae2f9' }}>
                <ApartmentOutlined className="stat-icon" />
                <div className="stat-num">{countByPrefix(paths, 'ontology/logic/')}</div>
                <div className="stat-label">业务逻辑</div>
              </div>
              <div className="stat-tile" style={{ '--tile': '#0f8f7f', '--tile-bg': '#d5f3ee' }}>
                <FileSearchOutlined className="stat-icon" />
                <div className="stat-num">{countByPrefix(paths, 'research/')}</div>
                <div className="stat-label">调研报告</div>
              </div>
              <div className="stat-tile" style={{ '--tile': '#b77909', '--tile-bg': '#fbf0d7' }}>
                <HistoryOutlined className="stat-icon" />
                <div className="stat-num">{versions.length}</div>
                <div className="stat-label">版本数</div>
              </div>
            </div>
            {coverage && (coverage.design_coverage != null || coverage.pass_rate != null) && (
              <div className="stat-grid">
                <div className="stat-tile" style={{ '--tile': '#16845b', '--tile-bg': '#e2f5ed' }}>
                  <div className="stat-num">
                    {coverage.design_coverage != null ? `${coverage.design_coverage}%` : '—'}
                  </div>
                  <div className="stat-label">设计覆盖率</div>
                </div>
                <div className="stat-tile" style={{ '--tile': '#256fd0', '--tile-bg': '#e8f1fd' }}>
                  <div className="stat-num">
                    {coverage.pass_rate != null ? `${coverage.pass_rate}%` : '—'}
                  </div>
                  <div className="stat-label">推演通过率</div>
                </div>
                <div
                  className="stat-tile"
                  style={{ '--tile': 'var(--text-secondary)', '--tile-bg': 'var(--surface-subtle)', cursor: 'pointer' }}
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
                  <span>
                    累计成本 <b>${usage.total_cost_usd.toFixed(2)}</b>
                  </span>
                  <span>调用 {usage.calls} 次</span>
                  <span>输出 {usage.output_tokens.toLocaleString()} tokens</span>
                </Space>
              </Card>
            )}
            <Card size="small" title="最近版本">
              {versions.slice(0, 3).map((v) => (
                <div key={v.commit}>
                  <Typography.Text type="secondary">版本 {v.version}</Typography.Text>{' '}
                  {v.message}
                </div>
              ))}
              {versions.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            </Card>
            <Typography.Paragraph type="secondary">
              从左栏「进度」开始建模；产物文件点击后在「文件」中预览与编辑。
            </Typography.Paragraph>
          </Space>
        </div>
      )}

      {focus === 'file' && (
        <div className="center-anim" key="file">
        <Spin spinning={loading}>
          {!file && <Empty description="从左栏「产物」选择文件" />}
          {file && /\.html?$/i.test(file) && (
            <>
              <Typography.Title level={5}>{file}</Typography.Title>
              <iframe
                className="html-preview"
                title={file}
                sandbox="allow-scripts"
                src={api.rawUrl(project.id, file)}
              />
            </>
          )}
          {file && content?.unsupported && !/\.html?$/i.test(file) && (
            <Empty description={`${file} 暂不支持预览（当前仅支持文本类文件）`} />
          )}
          {file && content?.error && <Empty description={content.error} />}
          {file && content?.content != null && (
            <>
              <Space style={{ marginBottom: 8 }} align="center">
                <Typography.Title level={5} style={{ margin: 0 }}>
                  {file}
                </Typography.Title>
                {content.truncated && <Tag color="warning">仅显示前 512KB</Tag>}
                {!editing ? (
                  <Button size="small" onClick={() => setEditing(true)}>
                    编辑
                  </Button>
                ) : (
                  <>
                    <Button
                      size="small"
                      type="primary"
                      onClick={() =>
                        saveDraft(draft, () => {
                          setContent({ ...content, content: draft });
                          setEditing(false);
                        })
                      }
                    >
                      保存
                    </Button>
                    <Button size="small" onClick={() => setEditing(false)}>
                      取消
                    </Button>
                  </>
                )}
              </Space>
              {editing ? (
                <CodeEditor key={file} value={draft} onChange={setDraft} />
              ) : /\.mmd$/i.test(file) ? (
                <MermaidBlock code={content.content} />
              ) : (
                <Markdown content={content.content} onWikiLink={openWikiLink} />
              )}
            </>
          )}
        </Spin>
        </div>
      )}

      {focus === 'diff' && (
        <div className="center-anim" key="diff">
          <Space style={{ marginBottom: 8 }} wrap>
            <Typography.Text>{file || '先从「产物」选择要对比的文件'}</Typography.Text>
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
            {diffState && (
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
                {reportPaths.map((path) => (
                  <div
                    key={path}
                    className={`report-item${reportPath === path ? ' active' : ''}`}
                    onClick={() => setReportPath(path)}
                  >
                    {path.replace('reports/', '')}
                  </div>
                ))}
              </div>
              <div className="report-body">
                {!reportPath && <Empty description="选择左侧报告查看" />}
                {reportPath && reportContent?.error && <Empty description={reportContent.error} />}
                {reportPath && reportContent?.content != null && (
                  <>
                    <Space style={{ marginBottom: 8 }} wrap>
                      <Button
                        size="small"
                        type="primary"
                        onClick={() =>
                          onCommand?.(
                            `请阅读 ${reportPath}，逐条修复其中列出的错误项，修复后重新运行校验`,
                          )
                        }
                      >
                        让助手修复错误项
                      </Button>
                      <Button size="small" onClick={() => onCommand?.('/ontology-validate')}>
                        重新校验
                      </Button>
                      <Button size="small" onClick={() => onOpenFile(reportPath)}>
                        在编辑器中打开
                      </Button>
                    </Space>
                    <Markdown content={reportContent.content} onWikiLink={openWikiLink} />
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
