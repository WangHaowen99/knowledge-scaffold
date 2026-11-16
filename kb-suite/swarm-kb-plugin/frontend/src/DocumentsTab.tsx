import { FileText, Globe, RefreshCw, RotateCcw, Search, Settings2, Trash2, Type, Upload } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import DocDrawer from './DocDrawer';
import { FILE_GROUPS } from './KbConfigForm';
import { ENRICH_LABEL, enrichBusy, enrichItems, formatBytes, formatTime, sourceLabel, statusOf } from './format';
import { has, nativeConfig, useBackends } from './backend';
import { useConfigMeta } from './meta';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { DocumentItem, KnowledgeBase } from './types';
import { unsupportedType } from './fileTypes';
import { ACCEPT, MAX_FILE_MB, uploadFile, type ProcessOverrides } from './upload';
import { Badge, Button, Empty, Field, Modal, NumberField, Spinner } from './ui';

type Tri = 'inherit' | 'on' | 'off';
const triOf = (v: unknown): Tri => (v === true ? 'on' : v === false ? 'off' : 'inherit');

function TriSelect({ label, value, onChange, base }: { label: string; value: Tri; onChange: (v: Tri) => void; base: boolean }) {
  return (
    <Field label={label}>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value as Tri)}>
        <option value="inherit">跟随知识库（{base ? '开' : '关'}）</option>
        <option value="on">开启</option>
        <option value="off">关闭</option>
      </select>
    </Field>
  );
}

function UploadOptionsModal({
  kb,
  value,
  onClose,
  onSave,
}: {
  kb: KnowledgeBase;
  value: ProcessOverrides | null;
  onClose: () => void;
  onSave: (v: ProcessOverrides | null) => void;
}) {
  const meta = useConfigMeta(nativeConfig(kb) ? kb.backend?.id ?? '' : null);
  const cfg = kb.config;
  const v = value ?? {};
  const [chunkSize, setChunkSize] = useState<number>(Number(v.chunking?.chunk_size ?? cfg.chunking.chunk_size));
  const [overlap, setOverlap] = useState<number>(Number(v.chunking?.chunk_overlap ?? cfg.chunking.chunk_overlap));
  const [customChunk, setCustomChunk] = useState(!!v.chunking);
  const [summary, setSummary] = useState<Tri>(triOf(v.summary?.enabled));
  const [questions, setQuestions] = useState<Tri>(triOf(v.questions?.enabled));
  const [count, setCount] = useState<number>(Number(v.questions?.count ?? cfg.questions.count));
  const [wiki, setWiki] = useState<Tri>(triOf(v.wiki?.enabled));
  const [rules, setRules] = useState<Record<string, string>>((v.parsing?.engine_rules as Record<string, string>) ?? {});
  const engines = (meta?.parser_engines ?? []).filter((e) => e.available);

  const build = (): ProcessOverrides | null => {
    const out: ProcessOverrides = {};
    if (customChunk) out.chunking = { chunk_size: chunkSize, chunk_overlap: overlap };
    if (summary !== 'inherit') out.summary = { enabled: summary === 'on' };
    if (questions !== 'inherit' || count !== cfg.questions.count) {
      out.questions = { ...(questions !== 'inherit' ? { enabled: questions === 'on' } : {}), count };
    }
    if (wiki !== 'inherit') out.wiki = { enabled: wiki === 'on' };
    if (Object.keys(rules).length) out.parsing = { engine_rules: rules };
    return Object.keys(out).length ? out : null;
  };

  return (
    <Modal
      title="上传选项"
      width={620}
      onClose={onClose}
      footer={
        <>
          <Button onClick={() => onSave(null)}>全部跟随知识库</Button>
          <Button variant="primary" onClick={() => onSave(build())}>
            应用到之后的上传
          </Button>
        </>
      }
    >
      <div className="field-hint" style={{ marginBottom: 12 }}>
        只对这次打开页面后上传、导入的文档生效，覆盖知识库设置里的对应项；文档详情的「解析信息」里能看到用了哪些自定义项。
      </div>
      <label className="check">
        <input type="checkbox" checked={customChunk} onChange={(e) => setCustomChunk(e.target.checked)} /> 自定义分块
      </label>
      {customChunk ? (
        <div className="row">
          <NumberField label="分块大小" min={100} max={4000} step={50} value={chunkSize} onChange={setChunkSize} />
          <NumberField label="分块重叠" min={0} max={Math.floor(chunkSize / 2)} step={10} value={overlap} onChange={setOverlap} />
        </div>
      ) : null}
      <div className="row">
        <TriSelect label="生成摘要" value={summary} onChange={setSummary} base={cfg.summary.enabled} />
        <TriSelect label="Wiki" value={wiki} onChange={setWiki} base={cfg.wiki.enabled} />
      </div>
      <div className="row">
        <TriSelect label="AI 问题生成" value={questions} onChange={setQuestions} base={cfg.questions.enabled} />
        <NumberField label="每个分块的问题数" min={1} max={10} value={count} onChange={setCount} />
      </div>
      <Field label="解析引擎">
        <div className="engine-grid">
          {FILE_GROUPS.slice(0, 5).map((g) => (
            <label key={g.label} className="engine-cell">
              <span>{g.label}</span>
              <select
                className="select"
                value={rules[g.types[0]] ?? 'inherit'}
                onChange={(e) =>
                  setRules((cur) => {
                    const next = { ...cur };
                    for (const t of g.types) {
                      if (e.target.value === 'inherit') delete next[t];
                      else next[t] = e.target.value;
                    }
                    return next;
                  })
                }
              >
                <option value="inherit">跟随知识库</option>
                <option value="auto">自动</option>
                {engines
                  .filter((en) => g.types.some((t) => en.file_types.includes(t)))
                  .map((en) => (
                    <option key={en.name} value={en.name}>
                      {en.name}
                    </option>
                  ))}
              </select>
            </label>
          ))}
        </div>
      </Field>
    </Modal>
  );
}

type UploadState = { id: number; name: string; progress: number; error?: string; done?: boolean };

const STATUS_FILTERS = [
  ['', '全部状态'],
  ['completed', '已完成'],
  ['processing', '处理中'],
  ['pending', '排队中'],
  ['failed', '失败'],
] as const;

function UrlModal({ kbId, overrides, onClose, onDone }: { kbId: string; overrides: ProcessOverrides | null; onClose: () => void; onDone: () => void }) {
  const notify = useToast();
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await rpc.call('docs.url', { kb_id: kbId, url: url.trim(), process_overrides: overrides ?? undefined });
      notify('已加入解析队列');
      onDone();
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="导入网页"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={!/^https?:\/\//.test(url.trim()) || busy} onClick={submit}>
            导入
          </Button>
        </>
      }
    >
      <Field label="网址" hint="由解析服务抓取页面正文。">
        <input className="input" autoFocus placeholder="https://" value={url} onChange={(e) => setUrl(e.target.value)} />
      </Field>
    </Modal>
  );
}

function TextModal({ kbId, overrides, onClose, onDone }: { kbId: string; overrides: ProcessOverrides | null; onClose: () => void; onDone: () => void }) {
  const notify = useToast();
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await rpc.call('docs.text', { kb_id: kbId, title: title.trim(), text, process_overrides: overrides ?? undefined });
      notify('已加入解析队列');
      onDone();
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="粘贴文本"
      width={620}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={!text.trim() || busy} onClick={submit}>
            保存
          </Button>
        </>
      }
    >
      <Field label="标题">
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
      </Field>
      <Field label="内容" hint="支持 Markdown。">
        <textarea className="textarea" style={{ minHeight: 240 }} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
    </Modal>
  );
}

export default function DocumentsTab({ kb, onChanged }: { kb: KnowledgeBase; onChanged: () => void }) {
  const notify = useToast();
  const [docs, setDocs] = useState<DocumentItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('');
  const [uploads, setUploads] = useState<UploadState[]>([]);
  const [dragging, setDragging] = useState(false);
  const [modal, setModal] = useState<'url' | 'text' | 'options' | null>(null);
  const [overrides, setOverrides] = useState<ProcessOverrides | null>(null);
  const [openDoc, setOpenDoc] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const uploadSeq = useRef(0);

  // the knowledge base's counts (header, sidebar) follow the list: refresh them whenever its total changes
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const lastTotal = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await rpc.call<{ items: DocumentItem[]; total: number }>('docs.list', {
        kb_id: kb.id,
        q: query.trim() || undefined,
        status: status || undefined,
        limit: 200,
      });
      setDocs(res.items);
      setTotal(res.total);
      if (!query.trim() && !status) {
        if (lastTotal.current !== null && lastTotal.current !== res.total) onChangedRef.current();
        lastTotal.current = res.total;
      }
    } catch (err) {
      setDocs((cur) => cur ?? []);
      notify(errorText(err), true);
    }
  }, [kb.id, query, status, notify]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 200);
    return () => window.clearTimeout(timer);
  }, [load]);

  const busy = (docs ?? []).some((d) => statusOf(d).busy || enrichBusy(d));
  useEffect(() => {
    const timer = window.setInterval(() => {
      void load();
      if (busy) onChanged();
    }, busy ? 3000 : 15000);
    return () => window.clearInterval(timer);
  }, [busy, load, onChanged]);

  // a backend behind kb-gateway declares its own file types and size limit
  const backend = useBackends()?.find((b) => b.id === kb.backend?.id);
  const accept = backend?.file_types.length ? backend.file_types.map((t) => `.${t}`).join(',') : ACCEPT;
  const maxMb = backend?.limits.max_upload_mb || MAX_FILE_MB;
  const uploadHint = backend?.file_types.length
    ? `支持 ${backend.file_types.map((t) => t.toUpperCase()).join('、')}，单个不超过 ${maxMb}MB`
    : `支持 PDF、Word、PPT、Excel、CSV、Markdown、TXT、HTML，单个不超过 ${maxMb}MB`;

  const startUploads = async (files: FileList | File[]) => {
    const list = Array.from(files);
    for (const file of list) {
      const id = ++uploadSeq.current;
      // a dropped file skips the picker's type filter: refuse it here instead of uploading it first
      const refused = unsupportedType(file.name, backend?.file_types);
      if (refused) {
        setUploads((cur) => [...cur, { id, name: file.name, progress: 0, error: refused }]);
        continue;
      }
      setUploads((cur) => [...cur, { id, name: file.name, progress: 0 }]);
      try {
        await uploadFile(
          kb.id,
          file,
          (ratio) => setUploads((cur) => cur.map((u) => (u.id === id ? { ...u, progress: ratio } : u))),
          overrides,
          maxMb,
        );
        setUploads((cur) => cur.map((u) => (u.id === id ? { ...u, progress: 1, done: true } : u)));
        window.setTimeout(() => setUploads((cur) => cur.filter((u) => u.id !== id)), 2500);
      } catch (err) {
        setUploads((cur) => cur.map((u) => (u.id === id ? { ...u, error: errorText(err) } : u)));
      }
      void load();
      onChanged();
    }
  };

  const act = async (method: string, doc: DocumentItem, done: string) => {
    try {
      await rpc.call(method, { doc_id: doc.id });
      notify(done);
      void load();
      onChanged();
      if (method === 'docs.delete') {
        // some backends (WeKnora) delete in the background: refresh the counts again once it has finished
        for (const ms of [2000, 6000]) {
          window.setTimeout(() => {
            void load();
            onChanged();
          }, ms);
        }
      }
    } catch (err) {
      notify(errorText(err), true);
    }
  };

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        if (e.dataTransfer.files.length && has(kb, 'ingest.file')) void startUploads(e.dataTransfer.files);
      }}
    >
      <div className="toolbar">
        {has(kb, 'ingest.file') ? (
          <Button variant="primary" icon={<Upload size={14} />} onClick={() => fileInput.current?.click()}>
            上传文件
          </Button>
        ) : null}
        {has(kb, 'ingest.url') ? (
          <Button icon={<Globe size={14} />} onClick={() => setModal('url')}>
            导入网页
          </Button>
        ) : null}
        {has(kb, 'ingest.text') ? (
          <Button icon={<Type size={14} />} onClick={() => setModal('text')}>
            粘贴文本
          </Button>
        ) : null}
        {nativeConfig(kb) ? (
          <Button variant={overrides ? 'secondary' : 'ghost'} icon={<Settings2 size={14} />} onClick={() => setModal('options')}>
            上传选项{overrides ? '（已自定义）' : ''}
          </Button>
        ) : null}
        <span className="grow" />
        <div className="search-box">
          <Search size={14} />
          <input className="input" placeholder="按标题筛选" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <select className="select" style={{ width: 110 }} value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUS_FILTERS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => void load()} title="刷新" />
        <input
          ref={fileInput}
          type="file"
          multiple
          accept={accept}
          hidden
          onChange={(e) => {
            if (e.target.files?.length) void startUploads(e.target.files);
            e.target.value = '';
          }}
        />
      </div>

      {has(kb, 'ingest.file') ? (
        <div className={`dropzone ${dragging ? 'dropzone-active' : ''}`}>
          把文件拖到这里上传 · {uploadHint}
        </div>
      ) : null}

      {uploads.length ? (
        <div className="uploads">
          {uploads.map((u) => (
            <div className="upload-row" key={u.id}>
              <FileText size={14} className="muted" />
              <span className="upload-name">{u.name}</span>
              {u.error ? (
                <Badge tone="danger" title={u.error}>
                  {u.error}
                </Badge>
              ) : u.done ? (
                <Badge tone="success">已上传</Badge>
              ) : (
                <div className="progress">
                  <div style={{ width: `${Math.round(u.progress * 100)}%` }} />
                </div>
              )}
              {u.error ? (
                <button className="link-btn" onClick={() => setUploads((cur) => cur.filter((x) => x.id !== u.id))}>
                  移除
                </button>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}

      {docs === null ? (
        <div className="empty">
          <Spinner size={18} />
        </div>
      ) : docs.length === 0 ? (
        <Empty
          icon={<FileText size={36} />}
          title={query || status ? '没有符合条件的文档' : '这个知识库还没有文档'}
          hint={query || status ? undefined : '上传文件、导入网页或粘贴文本开始。'}
        />
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th>文档</th>
                <th>类型</th>
                <th>状态</th>
                <th style={{ textAlign: 'right' }}>分块</th>
                <th>更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {docs.map((doc) => {
                const st = statusOf(doc);
                return (
                  <tr key={doc.id} onClick={() => setOpenDoc(doc.id)}>
                    <td>
                      <div className="doc-title" title={doc.title}>
                        {doc.title}
                      </div>
                      <div className="doc-sub">
                        {doc.source_type === 'url' ? doc.source_url : doc.file_name ?? '粘贴文本'}
                        {doc.file_size ? ` · ${formatBytes(doc.file_size)}` : ''}
                      </div>
                    </td>
                    <td className="muted">{sourceLabel(doc)}</td>
                    <td>
                      <Badge tone={st.tone} title={doc.error_message ?? undefined}>
                        {st.busy ? <Spinner size={10} /> : null}
                        {st.label}
                      </Badge>
                      {doc.parse_status === 'completed'
                        ? enrichItems(doc)
                            .filter((e) => e.status !== 'completed')
                            .map((e) => (
                              <span key={e.key} className={`enrich enrich-${e.tone}`} title={e.key === 'wiki' ? doc.wiki_error ?? undefined : undefined}>
                                {e.label}
                                {ENRICH_LABEL[e.status] ?? e.status}
                              </span>
                            ))
                        : null}
                      {doc.question_count ? <span className="enrich enrich-neutral">{doc.question_count} 问</span> : null}
                    </td>
                    <td className="num" style={{ textAlign: 'right' }}>
                      {doc.chunk_count || '—'}
                    </td>
                    <td className="muted">{formatTime(doc.processed_at ?? doc.created_at)}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="actions">
                        {has(kb, 'documents.reprocess') ? (
                          <button
                            className="icon-btn"
                            title="重新解析"
                            onClick={() => void act('docs.reprocess', doc, '已重新排队')}
                          >
                            <RotateCcw size={14} />
                          </button>
                        ) : null}
                        {has(kb, 'documents.delete') ? (
                          <button
                            className="icon-btn"
                            title="删除"
                            onClick={() => {
                              if (window.confirm(`删除「${doc.title}」？删除后不可恢复。`)) void act('docs.delete', doc, '已删除');
                            }}
                          >
                            <Trash2 size={14} />
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {total > docs.length ? <div className="muted" style={{ marginTop: 10 }}>仅显示最近 {docs.length} 个，共 {total} 个</div> : null}
        </>
      )}

      {modal === 'options' ? (
        <UploadOptionsModal
          kb={kb}
          value={overrides}
          onClose={() => setModal(null)}
          onSave={(v) => {
            setOverrides(v);
            setModal(null);
          }}
        />
      ) : null}
      {modal === 'url' ? (
        <UrlModal
          kbId={kb.id}
          overrides={overrides}
          onClose={() => setModal(null)}
          onDone={() => {
            setModal(null);
            void load();
            onChanged();
          }}
        />
      ) : null}
      {modal === 'text' ? (
        <TextModal
          kbId={kb.id}
          overrides={overrides}
          onClose={() => setModal(null)}
          onDone={() => {
            setModal(null);
            void load();
            onChanged();
          }}
        />
      ) : null}
      {openDoc ? (
        <DocDrawer
          docId={openDoc}
          features={kb.features}
          onClose={() => setOpenDoc(null)}
          onChanged={() => {
            void load();
            onChanged();
          }}
        />
      ) : null}
    </div>
  );
}
