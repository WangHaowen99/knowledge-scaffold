import { BookOpen, Network, Pencil, RefreshCw, Search, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import DocDrawer from './DocDrawer';
import { formatTime } from './format';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { KnowledgeBase, WikiGraph, WikiPage, WikiPageItem, WikiPageType, WikiStatus } from './types';
import { Badge, Button, Empty, Field, Spinner, Tabs } from './ui';

const TYPE_LABEL: Record<WikiPageType, string> = { index: '索引', summary: '文档', entity: '实体', concept: '概念' };
const TYPE_TONE: Record<WikiPageType, 'neutral' | 'info' | 'success' | 'warning'> = {
  index: 'neutral',
  summary: 'warning',
  entity: 'info',
  concept: 'success',
};
const DOC_STATUS: Record<string, string> = {
  completed: '已生成',
  processing: '生成中',
  pending: '排队中',
  failed: '失败',
  none: '未处理',
};
const LINK_RE = /\[\[([^[\]|\n]+?)(?:\|([^[\]\n]+?))?\]\]/g;
const plain = (text: string) => text.replace(LINK_RE, (_, slug: string, label?: string) => label ?? slug);

function WikiMarkdown({ text, onLink }: { text: string; onLink: (slug: string) => void }) {
  const md = text.replace(LINK_RE, (_, slug: string, label?: string) => `[${label ?? slug}](#wiki:${encodeURIComponent(slug.trim())})`);
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) =>
            href?.startsWith('#wiki:') ? (
              <a
                className="wiki-link"
                href={href}
                onClick={(e) => {
                  e.preventDefault();
                  onLink(decodeURIComponent(href.slice(6)));
                }}
              >
                {children}
              </a>
            ) : (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            ),
          img: ({ alt }) => <span className="img-ph">[图片{alt ? `：${alt}` : ''}]</span>,
        }}
      >
        {md}
      </ReactMarkdown>
    </div>
  );
}

function PageView({
  kbId,
  slug,
  onOpen,
  onDoc,
  onChanged,
}: {
  kbId: string;
  slug: string;
  onOpen: (slug: string) => void;
  onDoc: (docId: string) => void;
  onChanged?: () => void;
}) {
  const notify = useToast();
  const [page, setPage] = useState<WikiPage | null>(null);
  const [missing, setMissing] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ title: '', summary: '', content: '' });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setMissing(false);
      setPage(await rpc.call<WikiPage>('wiki.page', { kb_id: kbId, slug }));
    } catch (err) {
      setPage(null);
      setMissing(true);
      notify(errorText(err), true);
    }
  }, [kbId, slug, notify]);

  useEffect(() => {
    setEditing(false);
    void load();
  }, [load]);

  if (missing) return <Empty title="页面不存在" hint={slug} />;
  if (!page) {
    return (
      <div className="empty">
        <Spinner size={18} />
      </div>
    );
  }

  const save = async () => {
    setBusy(true);
    try {
      setPage(await rpc.call<WikiPage>('wiki.edit', { kb_id: kbId, slug, ...draft }));
      setEditing(false);
      notify('已保存');
      onChanged?.();
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`删除 Wiki 页面「${page.title}」？之后重建 Wiki 时可能会重新生成。`)) return;
    try {
      await rpc.call('wiki.delete', { kb_id: kbId, slug });
      notify('已删除');
      onChanged?.();
      setMissing(true);
    } catch (err) {
      notify(errorText(err), true);
    }
  };

  return (
    <div className="wiki-page">
      <div className="wiki-page-head">
        <div style={{ minWidth: 0 }}>
          <div className="wiki-page-title">
            <Badge tone={TYPE_TONE[page.page_type]}>{TYPE_LABEL[page.page_type]}</Badge>
            <span>{page.title}</span>
          </div>
          <div className="drawer-meta">
            <code>{page.slug}</code>
            <span>v{page.version}</span>
            <span>{page.last_edit_source === 'user' ? '人工编辑' : '自动生成'}</span>
            <span>更新于 {formatTime(page.updated_at)}</span>
            {page.aliases.length ? <span>别名：{page.aliases.join('、')}</span> : null}
          </div>
        </div>
        <div className="row-inline">
          {!editing ? (
            <Button
              size="sm"
              icon={<Pencil size={13} />}
              onClick={() => {
                setDraft({ title: page.title, summary: page.summary, content: page.content });
                setEditing(true);
              }}
            >
              编辑
            </Button>
          ) : null}
          <Button size="sm" variant="danger" icon={<Trash2 size={13} />} onClick={remove}>
            删除
          </Button>
        </div>
      </div>
      {editing ? (
        <div>
          <Field label="标题">
            <input className="input" value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
          </Field>
          <Field label="一句话摘要">
            <input className="input" value={draft.summary} onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
          </Field>
          <Field label="正文（Markdown，[[slug|文字]] 是页面链接）">
            <textarea className="textarea mono" style={{ minHeight: 360 }} value={draft.content} onChange={(e) => setDraft({ ...draft, content: e.target.value })} />
          </Field>
          <div className="row-inline">
            <Button onClick={() => setEditing(false)}>取消</Button>
            <Button variant="primary" disabled={busy || !draft.title.trim()} onClick={save}>
              {busy ? <Spinner /> : null}保存
            </Button>
          </div>
        </div>
      ) : (
        <>
          {page.summary ? <div className="wiki-summary">{plain(page.summary)}</div> : null}
          <WikiMarkdown text={page.content} onLink={onOpen} />
          <div className="wiki-refs">
            <div>
              <div className="field-label">链接到（{page.links_to.length}）</div>
              {page.links_to.map((r) => (
                <button key={r.slug} className="link-btn wiki-ref" onClick={() => onOpen(r.slug)}>
                  {r.title}
                </button>
              ))}
            </div>
            <div>
              <div className="field-label">被链接自（{page.linked_from.length}）</div>
              {page.linked_from.map((r) => (
                <button key={r.slug} className="link-btn wiki-ref" onClick={() => onOpen(r.slug)}>
                  {r.title}
                </button>
              ))}
            </div>
            <div>
              <div className="field-label">来源文档（{page.sources.length}）</div>
              {page.sources.map((d) => (
                <button key={d.id} className="link-btn wiki-ref" onClick={() => onDoc(d.id)}>
                  {d.title}
                </button>
              ))}
            </div>
          </div>
          {page.revisions.length ? (
            <details className="wiki-revisions">
              <summary>修订记录（{page.revisions.length}）</summary>
              {page.revisions.map((r) => (
                <div key={r.version} className="field-hint">
                  v{r.version} · {r.edit_source === 'user' ? '人工' : '自动'} · {formatTime(r.created_at)} · {r.chars} 字 · {r.summary}
                </div>
              ))}
            </details>
          ) : null}
        </>
      )}
    </div>
  );
}

export function WikiPageDrawer({ kbId, slug, onClose }: { kbId: string; slug: string; onClose: () => void }) {
  const [current, setCurrent] = useState(slug);
  const [doc, setDoc] = useState<string | null>(null);
  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer" aria-label="Wiki 页面">
        <div className="drawer-head" style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </div>
        <div className="drawer-body">
          <PageView kbId={kbId} slug={current} onOpen={setCurrent} onDoc={setDoc} />
        </div>
      </aside>
      {doc ? <DocDrawer docId={doc} onClose={() => setDoc(null)} onChanged={() => undefined} /> : null}
    </>
  );
}

const NODE_COLOR: Record<WikiPageType, string> = {
  index: 'var(--color-text-secondary)',
  summary: 'var(--color-feedback-warning)',
  entity: 'var(--color-action-primary)',
  concept: 'var(--color-feedback-success)',
};

function layout(graph: WikiGraph, width: number, height: number): Map<string, { x: number; y: number }> {
  const nodes = graph.nodes.map((n, i) => {
    const angle = (i / Math.max(1, graph.nodes.length)) * Math.PI * 2;
    return { slug: n.slug, x: width / 2 + Math.cos(angle) * width * 0.35, y: height / 2 + Math.sin(angle) * height * 0.35, vx: 0, vy: 0 };
  });
  const index = new Map(nodes.map((n, i) => [n.slug, i]));
  const edges = graph.edges.map((e) => [index.get(e.source), index.get(e.target)]).filter((e): e is [number, number] => e[0] !== undefined && e[1] !== undefined);
  const k = Math.sqrt((width * height) / Math.max(1, nodes.length)) * 0.6;
  for (let step = 0; step < 220; step += 1) {
    const cool = 1 - step / 220;
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const dx = nodes[i].x - nodes[j].x;
        const dy = nodes[i].y - nodes[j].y;
        const d2 = Math.max(dx * dx + dy * dy, 1);
        const f = (k * k) / d2;
        nodes[i].vx += dx * f * 0.05;
        nodes[i].vy += dy * f * 0.05;
        nodes[j].vx -= dx * f * 0.05;
        nodes[j].vy -= dy * f * 0.05;
      }
    }
    for (const [a, b] of edges) {
      const dx = nodes[b].x - nodes[a].x;
      const dy = nodes[b].y - nodes[a].y;
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const f = ((d - k) / d) * 0.06;
      nodes[a].vx += dx * f;
      nodes[a].vy += dy * f;
      nodes[b].vx -= dx * f;
      nodes[b].vy -= dy * f;
    }
    for (const n of nodes) {
      n.vx += (width / 2 - n.x) * 0.004;
      n.vy += (height / 2 - n.y) * 0.004;
      const v = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
      const limit = 12 * cool + 0.5;
      if (v > limit) {
        n.vx = (n.vx / v) * limit;
        n.vy = (n.vy / v) * limit;
      }
      n.x = Math.min(width - 20, Math.max(20, n.x + n.vx));
      n.y = Math.min(height - 20, Math.max(20, n.y + n.vy));
      n.vx *= 0.6;
      n.vy *= 0.6;
    }
  }
  return new Map(nodes.map((n) => [n.slug, { x: n.x, y: n.y }]));
}

function GraphView({ kbId, onOpen }: { kbId: string; onOpen: (slug: string) => void }) {
  const notify = useToast();
  const [graph, setGraph] = useState<WikiGraph | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const width = 900;
  const height = 560;
  useEffect(() => {
    rpc
      .call<WikiGraph>('wiki.graph', { kb_id: kbId, limit: 150 })
      .then(setGraph)
      .catch((err) => notify(errorText(err), true));
  }, [kbId, notify]);
  const pos = useMemo(() => (graph ? layout(graph, width, height) : null), [graph]);
  if (!graph || !pos) {
    return (
      <div className="empty">
        <Spinner size={18} />
      </div>
    );
  }
  if (!graph.nodes.length) return <Empty title="还没有页面" />;
  const near = new Set(hover ? [hover, ...graph.edges.filter((e) => e.source === hover || e.target === hover).flatMap((e) => [e.source, e.target])] : []);
  return (
    <div className="wiki-graph">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Wiki 链接图谱">
        {graph.edges.map((e, i) => {
          const a = pos.get(e.source);
          const b = pos.get(e.target);
          if (!a || !b) return null;
          const lit = hover && (e.source === hover || e.target === hover);
          return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`wiki-edge ${lit ? 'wiki-edge-lit' : ''}`} />;
        })}
        {graph.nodes.map((n) => {
          const p = pos.get(n.slug)!;
          const r = 4 + Math.min(10, Math.sqrt(n.degree) * 2);
          const dim = hover && !near.has(n.slug);
          return (
            <g
              key={n.slug}
              className="wiki-node"
              opacity={dim ? 0.25 : 1}
              onMouseEnter={() => setHover(n.slug)}
              onMouseLeave={() => setHover(null)}
              onClick={() => onOpen(n.slug)}
            >
              <circle cx={p.x} cy={p.y} r={r} fill={NODE_COLOR[n.page_type]} />
              {r > 7 || near.has(n.slug) ? (
                <text x={p.x + r + 3} y={p.y + 4}>
                  {n.title.length > 14 ? `${n.title.slice(0, 14)}…` : n.title}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
      <div className="wiki-legend">
        {(['summary', 'entity', 'concept'] as WikiPageType[]).map((t) => (
          <span key={t}>
            <i style={{ background: NODE_COLOR[t] }} />
            {TYPE_LABEL[t]}
          </span>
        ))}
        <span className="muted">节点越大链接越多；点节点打开页面</span>
      </div>
    </div>
  );
}

type Filter = 'all' | WikiPageType;

export default function WikiTab({ kb, onOpenSettings }: { kb: KnowledgeBase; onOpenSettings: () => void }) {
  const notify = useToast();
  const [status, setStatus] = useState<WikiStatus | null>(null);
  const [pages, setPages] = useState<WikiPageItem[] | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [q, setQ] = useState('');
  const [current, setCurrent] = useState<string | null>(null);
  const [view, setView] = useState<'page' | 'graph'>('page');
  const [doc, setDoc] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  const load = useCallback(async () => {
    try {
      const [st, list] = await Promise.all([
        rpc.call<WikiStatus>('wiki.status', { kb_id: kb.id }),
        rpc.call<{ items: WikiPageItem[] }>('wiki.pages', { kb_id: kb.id, type: filter === 'all' ? undefined : filter, q: q.trim() || undefined, limit: 500 }),
      ]);
      setStatus(st);
      setPages(list.items);
      setCurrent((cur) => cur ?? list.items[0]?.slug ?? null);
    } catch (err) {
      setPages((cur) => cur ?? []);
      notify(errorText(err), true);
    }
  }, [kb.id, filter, q, notify]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 200);
    return () => window.clearTimeout(timer);
  }, [load]);

  const active = !!status && (status.pending_jobs > 0 || (status.documents.processing ?? 0) > 0 || (status.documents.pending ?? 0) > 0);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, [active, load]);

  const rebuild = async (reset: boolean) => {
    if (reset && !window.confirm('清空现有 Wiki 页面（包括人工编辑）后按全部文档重新生成？')) return;
    setWorking(true);
    try {
      const res = await rpc.call<{ queued: number }>('wiki.rebuild', { kb_id: kb.id, reset });
      notify(`已排队 ${res.queued} 篇文档`);
      void load();
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setWorking(false);
    }
  };

  if (status && !status.enabled && !(pages ?? []).length) {
    return (
      <Empty
        icon={<BookOpen size={36} />}
        title="这个知识库还没有开启 Wiki"
        hint="开启后，大模型会把文档整理成相互链接的实体、概念和文档摘要页，agent 可以按主题浏览和跨文档综合。"
        action={
          <Button variant="primary" onClick={onOpenSettings}>
            去设置里开启
          </Button>
        }
      />
    );
  }

  const counts = status?.pages ?? {};
  return (
    <div>
      <div className="wiki-bar">
        <div className="wiki-bar-stats">
          {(['summary', 'entity', 'concept'] as WikiPageType[]).map((t) => (
            <span key={t}>
              {TYPE_LABEL[t]} <b className="num">{counts[t] ?? 0}</b>
            </span>
          ))}
          {status
            ? Object.entries(status.documents)
                .filter(([k, n]) => n && k !== 'completed')
                .map(([k, n]) => (
                  <Badge key={k} tone={k === 'failed' ? 'danger' : 'info'}>
                    {k === 'processing' || k === 'pending' ? <Spinner size={10} /> : null}
                    {DOC_STATUS[k] ?? k} {n} 篇
                  </Badge>
                ))
            : null}
          {status?.pending_jobs ? <span className="muted">后台任务 {status.pending_jobs} 个</span> : null}
          {!status?.enabled ? <Badge tone="warning">Wiki 已关闭，新文档不再生成</Badge> : null}
        </div>
        <div className="row-inline">
          <Button size="sm" icon={view === 'graph' ? <BookOpen size={13} /> : <Network size={13} />} onClick={() => setView(view === 'graph' ? 'page' : 'graph')}>
            {view === 'graph' ? '页面' : '图谱'}
          </Button>
          <Button size="sm" icon={<RefreshCw size={13} />} disabled={working || !status?.enabled} onClick={() => void rebuild(false)}>
            重建
          </Button>
          <Button size="sm" variant="ghost" disabled={working || !status?.enabled} onClick={() => void rebuild(true)}>
            清空重建
          </Button>
        </div>
      </div>
      {status?.errors.length ? (
        <div className="error-box" style={{ marginBottom: 12 }}>
          {status.errors.map((e) => (
            <div key={e.id}>
              《{e.title}》：{e.wiki_error}
            </div>
          ))}
        </div>
      ) : null}
      {view === 'graph' ? (
        <GraphView
          kbId={kb.id}
          onOpen={(slug) => {
            setCurrent(slug);
            setView('page');
          }}
        />
      ) : (
        <div className="wiki">
          <aside className="wiki-side">
            <Tabs<Filter>
              value={filter}
              onChange={setFilter}
              items={[
                { key: 'all', label: '全部' },
                { key: 'summary', label: '文档' },
                { key: 'entity', label: '实体' },
                { key: 'concept', label: '概念' },
              ]}
            />
            <div className="search-box" style={{ width: '100%', margin: '8px 0' }}>
              <Search size={14} />
              <input className="input" placeholder="按标题、摘要、别名筛选" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <div className="wiki-list">
              {pages === null ? (
                <Spinner />
              ) : pages.length === 0 ? (
                <div className="muted" style={{ padding: 8 }}>
                  {active ? '正在生成…' : '没有页面'}
                </div>
              ) : (
                pages.map((p) => (
                  <button key={p.slug} className={`wiki-item ${p.slug === current ? 'wiki-item-active' : ''}`} onClick={() => setCurrent(p.slug)}>
                    <div className="wiki-item-title">
                      <Badge tone={TYPE_TONE[p.page_type]}>{TYPE_LABEL[p.page_type]}</Badge>
                      <span>{p.title}</span>
                    </div>
                    <div className="wiki-item-sub">{plain(p.summary)}</div>
                  </button>
                ))
              )}
            </div>
          </aside>
          <section className="wiki-main">
            {current ? (
              <PageView kbId={kb.id} slug={current} onOpen={setCurrent} onDoc={setDoc} onChanged={() => void load()} />
            ) : (
              <Empty icon={<BookOpen size={32} />} title={active ? 'Wiki 生成中' : '选择左侧的页面'} hint={active ? '文档摘要完成后会逐篇生成，几分钟后刷新看看。' : undefined} />
            )}
          </section>
        </div>
      )}
      {doc ? <DocDrawer docId={doc} onClose={() => setDoc(null)} onChanged={() => undefined} /> : null}
    </div>
  );
}
