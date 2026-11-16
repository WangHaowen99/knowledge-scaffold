import { Download, MessageSquareText, Pencil, RotateCcw, Square, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ENRICH_LABEL, enrichBusy, enrichItems, formatBytes, formatTime, locatorText, sourceLabel, statusOf, summaryBusy } from './format';
import { has } from './backend';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { ChunkItem, DocumentItem } from './types';
import { Badge, Button, Empty, Spinner, Tabs } from './ui';

type View = 'summary' | 'content' | 'chunks' | 'info';

const L1_PREVIEW_CHARS = 60000;

function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ img: ({ alt }) => <span className="img-ph">[图片{alt ? `：${alt}` : ''}]</span> }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function ChunkCard({ chunk, onSaved, editable }: { chunk: ChunkItem; onSaved: (questions: string[]) => void; editable: boolean }) {
  const notify = useToast();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const where = locatorText(chunk.source_locators);
  const save = async () => {
    setBusy(true);
    try {
      const questions = draft.split('\n').map((q) => q.trim()).filter(Boolean);
      const res = await rpc.call<{ questions: string[] }>('chunks.questions', { chunk_id: chunk.id, questions });
      onSaved(res.questions);
      setEditing(false);
      notify('问题已更新');
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="chunk">
      <div className="chunk-head">
        <Badge>#{chunk.seq}</Badge>
        {chunk.chunk_type !== 'text' ? <Badge tone="info">{chunk.chunk_type}</Badge> : null}
        {chunk.line_start != null ? <span>第 {chunk.line_start} 行</span> : null}
        {where ? <span>· {where}</span> : null}
        {chunk.context_header ? <span className="chunk-header">· {chunk.context_header}</span> : null}
      </div>
      <div className={`chunk-content ${open ? '' : 'clamp'}`}>{chunk.content}</div>
      {chunk.content.length > 240 ? (
        <button className="link-btn" onClick={() => setOpen(!open)}>
          {open ? '收起' : '展开'}
        </button>
      ) : null}
      {editing ? (
        <div className="chunk-questions">
          <textarea className="textarea" style={{ minHeight: 80 }} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="每行一个问题；清空保存即删除" />
          <div className="row-inline" style={{ marginTop: 6 }}>
            <Button size="sm" onClick={() => setEditing(false)}>
              取消
            </Button>
            <Button size="sm" variant="primary" disabled={busy} onClick={save}>
              {busy ? <Spinner /> : null}保存
            </Button>
          </div>
        </div>
      ) : chunk.questions?.length ? (
        <div className="chunk-questions">
          {chunk.questions.map((q) => (
            <div key={q} className="chunk-question">
              <MessageSquareText size={12} />
              {q}
            </div>
          ))}
          {editable ? (
            <button
              className="link-btn"
              onClick={() => {
                setDraft(chunk.questions.join('\n'));
                setEditing(true);
              }}
            >
              <Pencil size={11} /> 编辑问题
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function DocDrawer({
  docId,
  onClose,
  onChanged,
  features,
}: {
  docId: string;
  onClose: () => void;
  onChanged: () => void;
  features?: string[];
}) {
  const can = (feature: string) => has({ features }, feature);
  const notify = useToast();
  const [doc, setDoc] = useState<DocumentItem | null>(null);
  const [view, setView] = useState<View>('summary');
  const [content, setContent] = useState<string | null>(null);
  const [chunks, setChunks] = useState<ChunkItem[]>([]);
  const [chunkTotal, setChunkTotal] = useState(0);
  const [loadingChunks, setLoadingChunks] = useState(false);

  const loadDoc = useCallback(async () => {
    try {
      setDoc(await rpc.call<DocumentItem>('docs.get', { doc_id: docId }));
    } catch (err) {
      notify(errorText(err), true);
    }
  }, [docId, notify]);

  useEffect(() => {
    void loadDoc();
  }, [loadDoc]);

  // another document opened in the same drawer: drop what was loaded for the previous one
  useEffect(() => {
    setDoc(null);
    setContent(null);
    setChunks([]);
    setChunkTotal(0);
  }, [docId]);

  const busy = doc ? statusOf(doc).busy || enrichBusy(doc) : false;
  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => void loadDoc(), 3000);
    return () => window.clearInterval(timer);
  }, [busy, loadDoc]);

  const completed = doc?.parse_status === 'completed';

  useEffect(() => {
    if (view !== 'content' || !completed || content !== null) return;
    rpc
      .call<string>('docs.content', { doc_id: docId, level: 'L1' })
      .then((text) => setContent(text))
      .catch((err) => notify(errorText(err), true));
  }, [view, completed, content, docId, notify]);

  const loadChunks = useCallback(
    async (offset: number) => {
      setLoadingChunks(true);
      try {
        const res = await rpc.call<{ items: ChunkItem[]; total: number }>('docs.chunks', { doc_id: docId, offset, limit: 30 });
        setChunks((cur) => (offset === 0 ? res.items : [...cur, ...res.items]));
        setChunkTotal(res.total);
      } catch (err) {
        notify(errorText(err), true);
      } finally {
        setLoadingChunks(false);
      }
    },
    [docId, notify],
  );

  useEffect(() => {
    if (view === 'chunks' && completed && chunks.length === 0) void loadChunks(0);
  }, [view, completed, chunks.length, loadChunks]);

  const act = async (method: string, done: string, close = false) => {
    try {
      await rpc.call(method, { doc_id: docId });
      notify(done);
      onChanged();
      if (close) onClose();
      else {
        setContent(null);
        setChunks([]);
        void loadDoc();
      }
    } catch (err) {
      notify(errorText(err), true);
    }
  };

  const download = async () => {
    if (!doc) return;
    try {
      const res = await rpc.call<{ content_type: string; data: string }>('docs.file', { doc_id: docId }, 300000);
      const bytes = Uint8Array.from(window.atob(res.data), (c) => c.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: res.content_type }));
      const a = document.createElement('a');
      a.href = url;
      a.download = doc.file_name ?? 'file';
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (err) {
      notify(errorText(err), true);
    }
  };

  const st = doc ? statusOf(doc) : null;

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer" aria-label="文档详情">
        {!doc ? (
          <div className="empty">
            <Spinner size={18} />
          </div>
        ) : (
          <>
            <div className="drawer-head">
              <div className="drawer-title-row">
                <div className="drawer-title">{doc.title}</div>
                <button className="icon-btn" onClick={onClose} aria-label="关闭">
                  <X size={16} />
                </button>
              </div>
              <div className="drawer-meta">
                {st ? (
                  <Badge tone={st.tone}>
                    {st.busy ? <Spinner size={10} /> : null}
                    {st.label}
                  </Badge>
                ) : null}
                <span>{sourceLabel(doc)}</span>
                {doc.file_size ? <span>{formatBytes(doc.file_size)}</span> : null}
                {doc.chunk_count ? <span>{doc.chunk_count} 个分块</span> : null}
                <span>更新于 {formatTime(doc.processed_at ?? doc.created_at)}</span>
                {enrichItems(doc).map((e) => (
                  <Badge key={e.key} tone={e.tone}>
                    {e.label} {ENRICH_LABEL[e.status] ?? e.status}
                  </Badge>
                ))}
              </div>
              {doc.parse_status === 'failed' && doc.error_message ? (
                <div className="error-box">
                  {doc.error_code ? `[${doc.error_code}] ` : ''}
                  {doc.error_message}
                </div>
              ) : null}
              <div className="drawer-actions">
                {doc.source_type === 'file' && can('documents.file') ? (
                  <Button size="sm" icon={<Download size={13} />} onClick={download}>
                    下载原文件
                  </Button>
                ) : null}
                {can('documents.reprocess') ? (
                  <Button size="sm" icon={<RotateCcw size={13} />} onClick={() => void act('docs.reprocess', '已重新排队')}>
                    重新解析
                  </Button>
                ) : null}
                {completed && can('questions') ? (
                  <Button size="sm" icon={<MessageSquareText size={13} />} onClick={() => void act('docs.questions_regenerate', '已排队生成问题')}>
                    重新生成问题
                  </Button>
                ) : null}
                {st?.busy && doc.parse_status !== 'deleting' && can('documents.cancel') ? (
                  <Button size="sm" icon={<Square size={13} />} onClick={() => void act('docs.cancel', '已取消')}>
                    取消处理
                  </Button>
                ) : null}
                {can('documents.delete') ? (
                  <Button
                    size="sm"
                    variant="danger"
                    icon={<Trash2 size={13} />}
                    onClick={() => {
                      if (window.confirm(`删除「${doc.title}」？删除后不可恢复。`)) void act('docs.delete', '已删除', true);
                    }}
                  >
                    删除
                  </Button>
                ) : null}
              </div>
              <Tabs<View>
                value={view}
                onChange={setView}
                items={[
                  { key: 'summary', label: '摘要' },
                  { key: 'content', label: '正文' },
                  { key: 'chunks', label: '分块' },
                  { key: 'info', label: '解析信息' },
                ]}
              />
            </div>
            <div className="drawer-body">
              {view === 'summary' ? (
                doc.summary ? (
                  <Markdown text={doc.summary} />
                ) : summaryBusy(doc) || st?.busy ? (
                  <Empty icon={<Spinner size={18} />} title="摘要生成中" hint="文档处理完成后会自动生成摘要。" />
                ) : (
                  <Empty title="暂无摘要" hint={doc.summary_status === 'failed' ? '摘要生成失败，可以重新解析。' : undefined} />
                )
              ) : null}
              {view === 'content' ? (
                !completed ? (
                  <Empty title="文档还没有处理完成" />
                ) : content === null ? (
                  <div className="empty">
                    <Spinner size={18} />
                  </div>
                ) : (
                  <>
                    <Markdown text={content.slice(0, L1_PREVIEW_CHARS)} />
                    {content.length > L1_PREVIEW_CHARS ? (
                      <div className="muted" style={{ marginTop: 12 }}>
                        正文共 {content.length} 字，这里只预览前 {L1_PREVIEW_CHARS} 字。
                      </div>
                    ) : null}
                  </>
                )
              ) : null}
              {view === 'chunks' ? (
                !completed ? (
                  <Empty title="文档还没有处理完成" />
                ) : (
                  <>
                    {chunks.map((c) => (
                      <ChunkCard
                        key={c.id}
                        chunk={c}
                        editable={can('questions')}
                        onSaved={(questions) => setChunks((cur) => cur.map((x) => (x.id === c.id ? { ...x, questions } : x)))}
                      />
                    ))}
                    {loadingChunks ? <Spinner /> : null}
                    {!loadingChunks && chunks.length < chunkTotal ? (
                      <Button size="sm" onClick={() => void loadChunks(chunks.length)}>
                        加载更多（{chunks.length}/{chunkTotal}）
                      </Button>
                    ) : null}
                  </>
                )
              ) : null}
              {view === 'info' ? (
                <dl className="info-grid">
                  <dt>文档 ID</dt>
                  <dd>{doc.id}</dd>
                  <dt>来源</dt>
                  <dd>{doc.source_type === 'url' ? doc.source_url : doc.file_name ?? '粘贴文本'}</dd>
                  <dt>解析引擎</dt>
                  <dd>{doc.engine ?? '—'}</dd>
                  <dt>正文字数</dt>
                  <dd>{doc.l1_chars ?? '—'}</dd>
                  <dt>分块数</dt>
                  <dd>{doc.chunk_count}</dd>
                  <dt>摘要状态</dt>
                  <dd>{ENRICH_LABEL[doc.summary_status] ?? doc.summary_status}</dd>
                  <dt>生成问题</dt>
                  <dd>
                    {ENRICH_LABEL[doc.question_status] ?? doc.question_status}
                    {doc.question_count ? `，共 ${doc.question_count} 个` : ''}
                  </dd>
                  <dt>Wiki</dt>
                  <dd>
                    {ENRICH_LABEL[doc.wiki_status] ?? doc.wiki_status}
                    {doc.wiki_error ? `：${doc.wiki_error}` : ''}
                  </dd>
                  <dt>上传时自定义</dt>
                  <dd>{Object.keys(doc.process_overrides ?? {}).length ? <code>{JSON.stringify(doc.process_overrides)}</code> : '无'}</dd>
                  <dt>创建时间</dt>
                  <dd>{formatTime(doc.created_at)}</dd>
                  <dt>完成时间</dt>
                  <dd>{formatTime(doc.processed_at)}</dd>
                </dl>
              ) : null}
            </div>
          </>
        )}
      </aside>
    </>
  );
}
