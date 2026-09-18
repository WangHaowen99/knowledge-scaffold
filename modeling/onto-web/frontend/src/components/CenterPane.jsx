import { Empty, Segmented, Spin, Tag, Timeline, Typography } from 'antd';
import mermaid from 'mermaid';
import { useEffect, useId, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../lib/api.js';

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

export function Markdown({ content, onWikiLink }) {
  // 先把 Obsidian [[链接]] 转成占位链接，再交给 react-markdown
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
}

const PREVIEWABLE = /\.(md|markdown|txt|py|json|yaml|yml|csv|mmd)$/i;

export default function CenterPane({ project, file, onOpenFile }) {
  const [view, setView] = useState('file');
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [versions, setVersions] = useState([]);

  useEffect(() => {
    if (!file) {
      setContent(null);
      return;
    }
    setView('file');
    if (!PREVIEWABLE.test(file)) {
      setContent({ unsupported: true });
      return;
    }
    setLoading(true);
    api
      .readFile(project.id, file)
      .then(setContent)
      .catch((error) => setContent({ error: error.message }))
      .finally(() => setLoading(false));
  }, [project.id, file]);

  useEffect(() => {
    if (view === 'versions') {
      api.versions(project.id).then((data) => setVersions(data.versions)).catch(() => {});
    }
  }, [view, project.id]);

  const openWikiLink = (name) => {
    // [[链接]] 在 object/ 与 logic/ 两个目录下解析（与 CLI 版规则一致）
    for (const dir of ['ontology/object', 'ontology/logic']) {
      const candidate = `${dir}/${name}.md`;
      api
        .readFile(project.id, candidate)
        .then(() => onOpenFile(candidate))
        .catch(() => {});
    }
  };

  return (
    <div>
      <Segmented
        value={view}
        onChange={setView}
        options={[
          { label: '文件预览', value: 'file' },
          { label: '版本历史', value: 'versions' },
        ]}
        style={{ marginBottom: 12 }}
      />
      {view === 'file' && (
        <Spin spinning={loading}>
          {!file && <Empty description="从左侧选择文件预览" />}
          {file && content?.unsupported && (
            <Empty description={`${file} 暂不支持预览（首片仅支持文本类文件）`} />
          )}
          {file && content?.error && <Empty description={content.error} />}
          {file && content?.content != null && (
            <>
              <Typography.Title level={5}>{file}</Typography.Title>
              {content.truncated && <Tag color="warning">文件过大，仅显示前 512KB</Tag>}
              {/\.mmd$/i.test(file) ? (
                <MermaidBlock code={content.content} />
              ) : (
                <Markdown content={content.content} onWikiLink={openWikiLink} />
              )}
            </>
          )}
        </Spin>
      )}
      {view === 'versions' && (
        <Timeline
          items={versions.map((entry) => ({
            children: (
              <>
                <Typography.Text strong>版本 {entry.version}</Typography.Text>{' '}
                <Typography.Text type="secondary">
                  {new Date(entry.time * 1000).toLocaleString()}
                </Typography.Text>
                <div>{entry.message}</div>
              </>
            ),
          }))}
        />
      )}
    </div>
  );
}
