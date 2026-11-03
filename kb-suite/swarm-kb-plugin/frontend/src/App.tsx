import { Database, Library, Plus } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import DocumentsTab from './DocumentsTab';
import GenericSettings from './GenericSettings';
import { EnrichSection, ParsingSection, WikiSection } from './KbConfigForm';
import SchemaForm, { schemaDefaults, schemaProblems } from './SchemaForm';
import SearchTab from './SearchTab';
import SettingsTab, { useConfigDraft } from './SettingsTab';
import WikiTab from './WikiTab';
import { has, nativeConfig, useBackends, type BackendInfo } from './backend';
import { useConfigMeta } from './meta';
import { rpc } from './rpc';
import { ToastProvider, errorText, useToast } from './toast';
import type { ConfigMeta, KnowledgeBase } from './types';
import { Button, Empty, Field, Modal, Spinner, Switch, Tabs } from './ui';

type Tab = 'docs' | 'wiki' | 'search' | 'settings';
type CreateSection = 'parsing' | 'enrich' | 'wiki';

function BackendPicker({ backends, value, onChange }: { backends: BackendInfo[]; value: string; onChange: (id: string) => void }) {
  if (backends.length < 2) return null;
  return (
    <Field label="知识后端" hint="知识库建在哪个后端上，之后不能更换。">
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        {backends.map((b) => (
          <option key={b.id} value={b.id} disabled={!b.healthy}>
            {b.label}
            {b.healthy ? '' : '（不可用）'}
          </option>
        ))}
      </select>
    </Field>
  );
}

function CreateKbModal({ onClose, onCreated }: { onClose: () => void; onCreated: (kb: KnowledgeBase) => void }) {
  const all = useBackends();
  const backends = useMemo(() => (all ?? []).filter((b) => b.features.includes('kbs.write')), [all]);
  const [picked, setPicked] = useState('');
  const backend = backends.find((b) => b.id === picked) ?? backends.find((b) => b.default && b.healthy) ?? backends.find((b) => b.healthy);
  // talking to kb-service directly (no gateway) there is no backend list and kb-service's own form applies
  const native = all !== null && (all.length === 0 || backend?.features.includes('config.meta'));
  const meta = useConfigMeta(native ? backend?.id ?? '' : null);
  const picker = <BackendPicker backends={backends} value={backend?.id ?? ''} onChange={setPicked} />;
  if (all !== null && all.length > 0 && !backend) {
    return (
      <Modal title="新建知识库" onClose={onClose}>
        <div className="empty-hint">没有可以新建知识库的知识后端。</div>
      </Modal>
    );
  }
  if (all === null || (native && !meta)) {
    return (
      <Modal title="新建知识库" onClose={onClose}>
        <div className="empty">
          <Spinner size={18} />
        </div>
      </Modal>
    );
  }
  if (!native && backend) {
    return <CreateGenericForm key={backend.id} backend={backend} picker={picker} onClose={onClose} onCreated={onCreated} />;
  }
  return <CreateKbForm key={backend?.id ?? ''} meta={meta as ConfigMeta} backend={backend?.id} picker={picker} onClose={onClose} onCreated={onCreated} />;
}

function CreateGenericForm({
  backend,
  picker,
  onClose,
  onCreated,
}: {
  backend: BackendInfo;
  picker: ReactNode;
  onClose: () => void;
  onCreated: (kb: KnowledgeBase) => void;
}) {
  const notify = useToast();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [config, setConfig] = useState<Record<string, unknown>>(() => schemaDefaults(backend.config_schema));
  const [busy, setBusy] = useState(false);
  const invalid = schemaProblems(backend.config_schema, config).length > 0;
  const submit = async () => {
    if (!name.trim() || invalid) return;
    setBusy(true);
    try {
      const kb = await rpc.call<KnowledgeBase>('kbs.create', {
        name: name.trim(),
        description,
        backend: backend.id,
        ...(backend.config_schema ? { config } : {}),
      });
      notify('知识库已创建');
      onCreated(kb);
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="新建知识库"
      width={560}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={!name.trim() || invalid || busy} onClick={submit}>
            {busy ? <Spinner /> : null}创建
          </Button>
        </>
      }
    >
      {picker}
      <Field label="名称">
        <input className="input" autoFocus value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="说明" hint="写清楚这个知识库装了什么，agent 会据此判断要不要检索它。">
        <textarea className="textarea" style={{ minHeight: 90 }} value={description} onChange={(e) => setDescription(e.target.value)} />
      </Field>
      {backend.config_schema ? <SchemaForm schema={backend.config_schema} value={config} onChange={setConfig} /> : null}
    </Modal>
  );
}

function CreateKbForm({
  meta,
  backend,
  picker,
  onClose,
  onCreated,
}: {
  meta: ConfigMeta;
  backend?: string;
  picker: ReactNode;
  onClose: () => void;
  onCreated: (kb: KnowledgeBase) => void;
}) {
  const notify = useToast();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [more, setMore] = useState<CreateSection | null>(null);
  const { config, patch, setLanguage } = useConfigDraft(meta.defaults);
  const submit = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const kb = await rpc.call<KnowledgeBase>('kbs.create', { name: name.trim(), description, config, backend });
      notify('知识库已创建');
      onCreated(kb);
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="新建知识库"
      width={more ? 760 : 520}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={!name.trim() || busy} onClick={submit}>
            {busy ? <Spinner /> : null}创建
          </Button>
        </>
      }
    >
      {picker}
      <Field label="名称">
        <input className="input" autoFocus value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="说明" hint="写清楚这个知识库装了什么，agent 会据此判断要不要检索它。">
        <textarea className="textarea" style={{ minHeight: 90 }} value={description} onChange={(e) => setDescription(e.target.value)} />
      </Field>
      <div className="create-switches">
        <Switch checked={config.indexing.rag_enabled} onChange={(v) => patch('indexing', { rag_enabled: v })} label="RAG 检索" hint="切分、向量化、建关键词索引" />
        <Switch checked={config.questions.enabled} disabled={!config.indexing.rag_enabled} onChange={(v) => patch('questions', { enabled: v })} label="AI 问题生成" hint="为分块生成问题，提高召回" />
        <Switch checked={config.wiki.enabled} onChange={(v) => patch('wiki', { enabled: v })} label="Wiki 知识库" hint="把文档整理成相互链接的页面" />
      </div>
      <Tabs<CreateSection | 'none'>
        value={more ?? 'none'}
        onChange={(k) => setMore(k === 'none' || k === more ? null : k)}
        items={[
          { key: 'none', label: '常用设置' },
          { key: 'parsing', label: '解析与分块' },
          { key: 'enrich', label: '文档增强' },
          { key: 'wiki', label: 'Wiki' },
        ]}
      />
      {more ? (
        <div className="create-more">
          {more === 'parsing' ? <ParsingSection config={config} meta={meta} patch={patch} setLanguage={setLanguage} /> : null}
          {more === 'enrich' ? <EnrichSection config={config} meta={meta} patch={patch} /> : null}
          {more === 'wiki' ? <WikiSection config={config} meta={meta} patch={patch} /> : null}
        </div>
      ) : (
        <div className="field-hint" style={{ marginTop: 8 }}>检索参数等更多设置建好后在「设置」里调整。</div>
      )}
    </Modal>
  );
}

function Shell() {
  const notify = useToast();
  const [kbs, setKbs] = useState<KnowledgeBase[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('docs');
  const [creating, setCreating] = useState(false);
  const [connected, setConnected] = useState(rpc.connected);
  const [unavailable, setUnavailable] = useState<{ id: string; label: string; error: string | null }[]>([]);
  const backends = useBackends();

  useEffect(() => {
    const off = rpc.onStatus(setConnected);
    // the socket opens on module load and may have connected before this effect ran
    setConnected(rpc.connected);
    return off;
  }, []);

  const loadKbs = useCallback(async () => {
    try {
      const res = await rpc.call<{ items: KnowledgeBase[]; unavailable_backends?: { id: string; label: string; error: string | null }[] }>(
        'kbs.list',
      );
      setKbs(res.items);
      setUnavailable(res.unavailable_backends ?? []);
      setSelected((cur) => (cur && res.items.some((k) => k.id === cur) ? cur : res.items[0]?.id ?? null));
    } catch (err) {
      setKbs((cur) => cur ?? []);
      notify(errorText(err), true);
    }
  }, [notify]);

  useEffect(() => {
    void loadKbs();
  }, [loadKbs]);

  const kb = useMemo(() => kbs?.find((k) => k.id === selected) ?? null, [kbs, selected]);
  const manyBackends = (backends?.length ?? 0) > 1;
  const shownTab: Tab = tab === 'wiki' && !has(kb, 'wiki.read') ? 'docs' : tab;
  const native = nativeConfig(kb);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-head">
          <span className="sidebar-title">知识库</span>
          <Button size="sm" variant="ghost" icon={<Plus size={14} />} onClick={() => setCreating(true)}>
            新建
          </Button>
        </div>
        <div className="kb-list">
          {kbs === null ? (
            <div className="empty">
              <Spinner />
            </div>
          ) : kbs.length === 0 ? (
            <div className="empty-hint muted" style={{ padding: 12 }}>
              还没有知识库
            </div>
          ) : (
            kbs.map((item) => (
              <button
                key={item.id}
                className={`kb-item ${item.id === selected ? 'kb-item-active' : ''}`}
                onClick={() => {
                  setSelected(item.id);
                  setTab('docs');
                }}
              >
                <Library size={16} className="kb-item-icon" />
                <span style={{ minWidth: 0 }}>
                  <div className="kb-item-name">
                    {item.name}
                    {manyBackends && item.backend ? <span className="kb-backend-tag">{item.backend.label}</span> : null}
                  </div>
                  <div className="kb-item-meta">
                    {item.stats.documents} 个文档{item.stats.chunks !== undefined ? ` · ${item.stats.chunks} 个分块` : ''}
                  </div>
                </span>
              </button>
            ))
          )}
        </div>
        {unavailable.length ? (
          <div className="sidebar-warn" title={unavailable.map((u) => `${u.label}：${u.error ?? ''}`).join('\n')}>
            {unavailable.map((u) => u.label).join('、')} 暂不可用，其上的知识库未列出
          </div>
        ) : null}
        <div className="sidebar-foot" title="插件后端连接状态">
          <span className={`dot ${connected ? '' : 'dot-off'}`} />
          {connected ? '知识服务已连接' : '正在连接…'}
        </div>
      </aside>

      <main className="main">
        {!kb ? (
          <Empty
            icon={<Database size={40} />}
            title={kbs && kbs.length === 0 ? '先建一个知识库' : '选择一个知识库'}
            hint="上传文档后会自动解析、切分并建立索引，Swarm 的 agent 可以通过 MCP 检索这些内容。"
            action={
              <Button variant="primary" icon={<Plus size={14} />} onClick={() => setCreating(true)}>
                新建知识库
              </Button>
            }
          />
        ) : (
          <>
            <div className="main-head">
              <div className="main-title-row">
                <div style={{ minWidth: 0 }}>
                  <h1 className="main-title">
                    {kb.name}
                    {manyBackends && kb.backend ? <span className="kb-backend-tag">{kb.backend.label}</span> : null}
                  </h1>
                  {kb.description ? <div className="main-desc">{kb.description}</div> : null}
                </div>
              </div>
              <div className="stats">
                {[
                  ['文档', kb.stats.documents],
                  ['已完成', kb.stats.completed],
                  ['处理中', kb.stats.processing],
                  ['失败', kb.stats.failed],
                  ...(kb.stats.chunks !== undefined ? [['分块', kb.stats.chunks]] : []),
                  ...((native && kb.config.questions.enabled) || kb.stats.questions ? [['生成问题', kb.stats.questions ?? 0]] : []),
                  ...((native && kb.config.wiki.enabled) || kb.stats.wiki_pages ? [['Wiki 页', kb.stats.wiki_pages ?? 0]] : []),
                  ...(kb.stats.enriching ? [['增强处理中', kb.stats.enriching]] : []),
                ].map(([label, value]) => (
                  <div key={label as string}>
                    <div className="stat-value num">{value}</div>
                    <div className="stat-label">{label}</div>
                  </div>
                ))}
              </div>
              <Tabs<Tab>
                value={shownTab}
                onChange={setTab}
                items={[
                  { key: 'docs', label: '文档' },
                  ...(has(kb, 'wiki.read') ? [{ key: 'wiki' as const, label: 'Wiki' }] : []),
                  { key: 'search', label: '检索测试' },
                  { key: 'settings', label: '设置' },
                ]}
              />
            </div>
            <div className="main-body">
              {shownTab === 'docs' ? <DocumentsTab key={kb.id} kb={kb} onChanged={loadKbs} /> : null}
              {shownTab === 'wiki' ? <WikiTab key={kb.id} kb={kb} onOpenSettings={() => setTab('settings')} /> : null}
              {shownTab === 'search' ? <SearchTab key={kb.id} kb={kb} /> : null}
              {shownTab === 'settings' && !native ? (
                <GenericSettings
                  // remounted once the backend's schema arrives, so its defaults fill the form
                  key={`${kb.id}:${Boolean(backends?.find((b) => b.id === kb.backend?.id)?.config_schema)}`}
                  kb={kb}
                  backend={backends?.find((b) => b.id === kb.backend?.id)}
                  onSaved={loadKbs}
                  onDeleted={async () => {
                    setSelected(null);
                    await loadKbs();
                  }}
                />
              ) : null}
              {shownTab === 'settings' && native ? (
                <SettingsTab
                  key={kb.id}
                  kb={kb}
                  onSaved={loadKbs}
                  onDeleted={async () => {
                    setSelected(null);
                    await loadKbs();
                  }}
                />
              ) : null}
            </div>
          </>
        )}
      </main>

      {creating ? (
        <CreateKbModal
          onClose={() => setCreating(false)}
          onCreated={async (created) => {
            setCreating(false);
            await loadKbs();
            setSelected(created.id);
          }}
        />
      ) : null}
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  );
}
