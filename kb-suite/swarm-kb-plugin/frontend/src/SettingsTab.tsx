import { useEffect, useMemo, useState } from 'react';
import BackendSettings from './BackendSettings';
import { offers, useKbScope } from './backend';
import { EnrichSection, Group, ParsingSection, RetrievalSection, WikiSection, type ConfigPatch } from './KbConfigForm';
import { formatTime } from './format';
import { useConfigMeta } from './meta';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { KbConfig, KnowledgeBase } from './types';
import { Button, Field, Spinner } from './ui';

type Section = 'basic' | 'parsing' | 'enrich' | 'wiki' | 'retrieval' | 'backend' | 'apply' | 'danger';

const NAV: { key: Section; label: string; group: string }[] = [
  { key: 'basic', label: '基本信息', group: '基础' },
  { key: 'parsing', label: '解析与分块', group: '索引与解析' },
  { key: 'enrich', label: '文档增强', group: '索引与解析' },
  { key: 'wiki', label: 'Wiki', group: '索引与解析' },
  { key: 'retrieval', label: '检索策略', group: '检索' },
  { key: 'apply', label: '应用到已有文档', group: '管理' },
  { key: 'danger', label: '删除知识库', group: '管理' },
];

const clone = (c: KbConfig): KbConfig => JSON.parse(JSON.stringify(c)) as KbConfig;

export const ADJUSTED_NOTICE = '已保存；服务端调整了部分设置（如不合法的值恢复为默认），页面已显示保存后的值';

/**
 * Save, then read the knowledge base back: the server may replace what it does not accept
 * (e.g. an empty separator list becomes its defaults), and the page must show what was kept.
 */
export async function saveKb(kbId: string, params: Record<string, unknown>): Promise<KnowledgeBase | null> {
  const updated = await rpc.call<KnowledgeBase | null>('kbs.update', { kb_id: kbId, ...params });
  try {
    return await rpc.call<KnowledgeBase>('kbs.get', { kb_id: kbId });
  } catch {
    return updated;
  }
}

export function useConfigDraft(initial: KbConfig) {
  const [config, setConfig] = useState<KbConfig>(() => clone(initial));
  const patch: ConfigPatch = (section, values) =>
    setConfig((cur) => ({ ...cur, [section]: { ...cur[section], ...values } }));
  const setLanguage = (language: string) => setConfig((cur) => ({ ...cur, language }));
  return { config, setConfig, patch, setLanguage };
}

export default function SettingsTab({
  kb,
  onSaved,
  onDeleted,
}: {
  kb: KnowledgeBase;
  onSaved: () => void;
  onDeleted: () => void;
}) {
  const notify = useToast();
  const meta = useConfigMeta(kb.backend?.id ?? '');
  const [section, setSection] = useState<Section>('basic');
  const [name, setName] = useState(kb.name);
  const [description, setDescription] = useState(kb.description);
  const { config, setConfig, patch, setLanguage } = useConfigDraft(kb.config);
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState(kb.profile);
  const [working, setWorking] = useState<string | null>(null);
  // what the server holds: the list's copy, or what was read back right after saving
  const [base, setBase] = useState<KnowledgeBase>(kb);
  useEffect(() => setBase(kb), [kb]);
  // backend-wide settings, when the backend declares them (contract 9.4)
  const backend = useKbScope()?.backend;
  const backendSchema = offers(kb, 'settings') ? backend?.settings_schema ?? null : null;
  const nav = backendSchema ? [...NAV.slice(0, 5), { key: 'backend' as const, label: backend?.label ?? '知识后端', group: '知识后端' }, ...NAV.slice(5)] : NAV;

  const dirty = useMemo(
    () => name !== base.name || description !== base.description || JSON.stringify(config) !== JSON.stringify(base.config),
    [name, description, config, base],
  );

  const showSaved = (saved: KnowledgeBase) => {
    setBase(saved);
    setName(saved.name);
    setDescription(saved.description);
    setConfig(clone(saved.config));
  };

  const save = async () => {
    setBusy(true);
    try {
      const sent = JSON.stringify(config);
      const saved = await saveKb(kb.id, { name: name.trim(), description, config });
      if (saved?.config) showSaved(saved);
      notify(saved?.config && JSON.stringify(saved.config) !== sent ? ADJUSTED_NOTICE : '已保存');
      onSaved();
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };

  const run = async (key: string, method: string, params: Record<string, unknown>, done: (res: unknown) => string) => {
    setWorking(key);
    try {
      const res = await rpc.call(method, { kb_id: kb.id, ...params }, 300000);
      notify(done(res));
      onSaved();
      return res;
    } catch (err) {
      notify(errorText(err), true);
      return null;
    } finally {
      setWorking(null);
    }
  };

  const remove = async () => {
    const typed = window.prompt(`删除知识库会同时删除其中全部文档、索引和 Wiki，且不可恢复。\n请输入知识库名称「${kb.name}」确认：`);
    if (typed === null) return;
    if (typed !== kb.name) {
      notify('输入的名称与知识库名称不一致，没有删除', true);
      return;
    }
    try {
      await rpc.call('kbs.delete', { kb_id: kb.id });
      notify('知识库已删除');
      onDeleted();
    } catch (err) {
      notify(errorText(err), true);
    }
  };

  const queued = (res: unknown) => `已排队 ${(res as { queued?: number } | null)?.queued ?? 0} 项`;
  const groups = Array.from(new Set(nav.map((n) => n.group)));

  return (
    <div className="settings">
      <nav className="settings-nav">
        {groups.map((g) => (
          <div key={g}>
            <div className="settings-nav-group">{g}</div>
            {nav.filter((n) => n.group === g).map((n) => (
              <button key={n.key} className={`settings-nav-item ${section === n.key ? 'settings-nav-active' : ''}`} onClick={() => setSection(n.key)}>
                {n.label}
              </button>
            ))}
          </div>
        ))}
      </nav>
      <div className="settings-body">
        {section === 'basic' ? (
          <>
            <Group title="基本信息">
              <Field label="名称">
                <input className="input" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field label="说明" hint="agent 会根据说明判断要不要检索这个知识库。">
                <textarea className="textarea" style={{ minHeight: 90 }} value={description} onChange={(e) => setDescription(e.target.value)} />
              </Field>
            </Group>
            <Group title="AI 生成的描述" hint="汇总各文档摘要自动生成；在「文档增强」里可以开启文档变化后自动更新。">
              {profile ? <div className="profile-box">{profile}</div> : <div className="muted" style={{ marginBottom: 10 }}>还没有生成过。</div>}
              {kb.profile_updated_at ? <div className="field-hint" style={{ marginBottom: 8 }}>更新于 {formatTime(kb.profile_updated_at)}</div> : null}
              <div className="row-inline">
                <Button
                  size="sm"
                  disabled={working !== null || kb.stats.completed === 0}
                  onClick={async () => {
                    const res = (await run('profile', 'kbs.profile_generate', {}, () => '描述已生成')) as { profile?: string } | null;
                    if (res?.profile) setProfile(res.profile);
                  }}
                >
                  {working === 'profile' ? <Spinner /> : null}
                  {profile ? '重新生成' : '生成描述'}
                </Button>
                <Button
                  size="sm"
                  disabled={!profile || working !== null}
                  onClick={async () => {
                    await run('apply', 'kbs.profile_apply', {}, () => '已用作知识库说明');
                    if (profile) setDescription(profile);
                  }}
                >
                  用作说明
                </Button>
              </div>
            </Group>
            <Group title="模型与存储">
              <dl className="info-grid">
                <dt>向量模型</dt>
                <dd>
                  {kb.embed_model}（{kb.embed_dim} 维，建库后不可更改）
                </dd>
                <dt>重排模型</dt>
                <dd>{meta?.models.rerank ?? '未配置'}</dd>
                <dt>默认对话模型</dt>
                <dd>{meta?.models.default_chat ?? '—'}</dd>
                <dt>向量存储</dt>
                <dd>ParadeDB（pgvector + BM25）</dd>
              </dl>
            </Group>
          </>
        ) : null}
        {section === 'parsing' ? <ParsingSection config={config} meta={meta} patch={patch} setLanguage={setLanguage} /> : null}
        {section === 'enrich' ? <EnrichSection config={config} meta={meta} patch={patch} /> : null}
        {section === 'wiki' ? <WikiSection config={config} meta={meta} patch={patch} /> : null}
        {section === 'retrieval' ? (
          <>
            <div className="field-hint" style={{ marginBottom: 12 }}>
              Swarm 的 agent（MCP）和 DA 检索这个知识库时都用这里的设置；「检索测试」页签可以临时改参数对比效果。
            </div>
            <RetrievalSection value={config.retrieval} onChange={(v) => patch('retrieval', v)} rerankModel={meta?.models.rerank} />
          </>
        ) : null}
        {section === 'backend' && backend && backendSchema ? <BackendSettings backend={backend} schema={backendSchema} /> : null}
        {section === 'apply' ? (
          <Group title="应用到已有文档" hint="设置保存后只对新文档生效；要让已有文档也按新设置处理，在这里批量重跑。">
            <div className="apply-list">
              {(
                [
                  ['all', '全部重新解析', '重新解析、切分、向量化全部文档，之后自动跑摘要、问题和 Wiki。分块或解析设置改了用这个。'],
                  ['summary', '重新生成摘要', '只重跑文档摘要。'],
                  ['questions', '重新生成问题', '只重跑 AI 问题生成（需要开启）。'],
                  ['wiki', '重建 Wiki', '按当前 Wiki 设置重新处理全部文档，已有页面会被增量合并。'],
                  ['profile', '更新知识库描述', '重新汇总文档摘要生成描述。'],
                ] as const
              ).map(([target, label, hint]) => (
                <div className="apply-row" key={target}>
                  <div>
                    <div className="field-label">{label}</div>
                    <div className="field-hint">{hint}</div>
                  </div>
                  <Button
                    size="sm"
                    disabled={working !== null || dirty}
                    title={dirty ? '先保存设置' : undefined}
                    onClick={() => {
                      if (target === 'all' && !window.confirm('全部文档都会重新解析，期间旧索引仍可检索。继续？')) return;
                      void run(target, 'kbs.reprocess', { target }, queued);
                    }}
                  >
                    {working === target ? <Spinner /> : null}执行
                  </Button>
                </div>
              ))}
            </div>
            {dirty ? <div className="field-hint" style={{ marginTop: 8 }}>有未保存的设置，先保存再执行。</div> : null}
          </Group>
        ) : null}
        {section === 'danger' ? (
          <div className="danger-zone" style={{ marginTop: 0 }}>
            <h3 className="section-title">删除知识库</h3>
            <div className="field-hint" style={{ marginBottom: 12 }}>
              删除后，其中的文档、分块、索引和 Wiki 都会被清除，agent 也无法再检索到。
            </div>
            <Button variant="danger" onClick={remove}>
              删除这个知识库
            </Button>
          </div>
        ) : null}
        {section !== 'apply' && section !== 'danger' && section !== 'backend' ? (
          <div className="settings-foot">
            <Button
              disabled={!dirty || busy}
              onClick={() => {
                setName(base.name);
                setDescription(base.description);
                setConfig(clone(base.config));
              }}
            >
              撤销修改
            </Button>
            <Button variant="primary" disabled={busy || !name.trim() || !dirty} onClick={save}>
              {busy ? <Spinner /> : null}保存
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
