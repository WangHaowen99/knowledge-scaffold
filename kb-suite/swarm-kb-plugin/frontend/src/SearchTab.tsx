import { BookOpen, Crosshair, Search, SlidersHorizontal } from 'lucide-react';
import { useMemo, useState } from 'react';
import DocDrawer from './DocDrawer';
import { RetrievalSection } from './KbConfigForm';
import SchemaForm, { schemaDefaults } from './SchemaForm';
import { has, nativeConfig, offers, useKbScope } from './backend';
import { locate } from './evidence';
import { locatorText } from './format';
import type { Focus } from './preview';
import { useConfigMeta } from './meta';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { KnowledgeBase, RetrievalConfig, SearchHit, SearchTrace } from './types';
import { Badge, Button, Empty, Spinner, Switch } from './ui';
import { WikiPageDrawer } from './WikiTab';

const MATCH_LABEL: Record<string, string> = {
  vector: '向量',
  keyword: '关键词',
  exact: '原样匹配',
  question: '问题',
  rerank: '重排',
  expansion: '查询扩展',
  merged: '已合并',
  expanded: '补上下文',
  parent: '父块',
  wiki: 'Wiki',
};

const COUNT_LABEL: [string, string][] = [
  ['vector', '向量召回'],
  ['keyword', '关键词召回'],
  ['question_vector', '问题向量'],
  ['question_keyword', '问题关键词'],
  ['wiki_vector', 'Wiki 向量'],
  ['wiki_keyword', 'Wiki 关键词'],
  ['fused', '融合后'],
  ['after_rerank', '重排过阈值'],
  ['selected', '入选'],
  ['returned', '返回'],
];

const fmt = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v.toFixed(3));

function Content({ hit }: { hit: SearchHit }) {
  if (!hit.core) return <div className="chunk-content clamp">{hit.content}</div>;
  const [a, b] = hit.core;
  return (
    <div className="chunk-content clamp-more">
      <span className="muted">{hit.content.slice(0, a)}</span>
      <mark className="core">{hit.content.slice(a, b)}</mark>
      <span className="muted">{hit.content.slice(b)}</span>
    </div>
  );
}

export default function SearchTab({ kb }: { kb: KnowledgeBase }) {
  const notify = useToast();
  const meta = useConfigMeta(nativeConfig(kb) ? kb.backend?.id ?? '' : null);
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState(8);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [trace, setTrace] = useState<SearchTrace | null>(null);
  const [busy, setBusy] = useState(false);
  const [openDoc, setOpenDoc] = useState<{ id: string; focus?: Focus } | null>(null);
  const [openWiki, setOpenWiki] = useState<string | null>(null);
  const [showParams, setShowParams] = useState(false);
  const [overrides, setOverrides] = useState<Partial<RetrievalConfig>>({});
  const [includeWiki, setIncludeWiki] = useState(true);
  const native = nativeConfig(kb);
  const baseRetrieval = native ? kb.config.retrieval : undefined;
  const params = useMemo(() => ({ ...baseRetrieval, ...overrides }) as RetrievalConfig, [baseRetrieval, overrides]);
  const nativeChanged = Object.keys(overrides).filter(
    (k) => overrides[k as keyof RetrievalConfig] !== baseRetrieval?.[k as keyof RetrievalConfig],
  );
  // another backend may declare what its search accepts in ``options`` (contract 9.5)
  const backend = useKbScope()?.backend;
  const optionsSchema = !native && offers(kb, 'search.options') ? backend?.search_options_schema ?? null : null;
  const optionDefaults = useMemo(() => schemaDefaults(optionsSchema), [optionsSchema]);
  const [optionEdits, setOptionEdits] = useState<Record<string, unknown> | null>(null);
  const optionValues = optionEdits ?? optionDefaults;
  const optionChanged = Object.keys(optionValues).filter(
    (k) => optionValues[k] !== undefined && JSON.stringify(optionValues[k]) !== JSON.stringify(optionDefaults[k]),
  );
  const changed = optionsSchema ? optionChanged : nativeChanged;
  const options = optionsSchema
    ? Object.fromEntries(optionChanged.map((k) => [k, optionValues[k]]))
    : Object.fromEntries(nativeChanged.map((k) => [k, overrides[k as keyof RetrievalConfig]]));

  const run = async () => {
    if (!query.trim()) return;
    setBusy(true);
    try {
      const res = await rpc.call<{ items: SearchHit[]; trace: SearchTrace }>('search', {
        query: query.trim(),
        kb_ids: [kb.id],
        top_k: topK,
        include_wiki: includeWiki,
        options: changed.length ? options : undefined,
        trace: true,
      });
      setHits(res.items);
      setTrace(res.trace);
    } catch (err) {
      notify(errorText(err), true);
    } finally {
      setBusy(false);
    }
  };

  // the anchored passage in the original file (or the text), via the backend's evidence when it has it
  const locateHit = async (hit: SearchHit) => {
    const focus = await locate(hit.anchor_id, { locators: hit.locators, line_start: hit.line_start }, offers(kb, 'evidence'));
    if (Object.keys(focus).length) setOpenDoc({ id: hit.doc_id, focus });
    else notify('无法定位', true);
  };

  return (
    <div>
      <div className="search-bar">
        <input
          className="input"
          placeholder="输入问题，测试检索效果（和 agent 调用的是同一套检索）"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void run()}
        />
        <select className="select" style={{ width: 100 }} value={topK} onChange={(e) => setTopK(Number(e.target.value))}>
          {[5, 8, 10, 20, 30].map((n) => (
            <option key={n} value={n}>
              前 {n} 条
            </option>
          ))}
        </select>
        <Button icon={<SlidersHorizontal size={14} />} onClick={() => setShowParams(!showParams)}>
          参数{changed.length ? `（改了 ${changed.length} 项）` : ''}
        </Button>
        <Button variant="primary" icon={busy ? <Spinner /> : <Search size={14} />} disabled={busy || !query.trim()} onClick={run}>
          检索
        </Button>
      </div>

      {showParams ? (
        <div className="param-panel">
          {optionsSchema ? (
            <div className="param-panel-head">
              <span />
              <Button size="sm" disabled={!changed.length} onClick={() => setOptionEdits(null)}>
                恢复默认
              </Button>
            </div>
          ) : (
            <div className="param-panel-head">
              <span className="field-hint">这里的修改只用于本页测试，不会保存；满意后到「设置 → 检索策略」里改。</span>
              <Button size="sm" disabled={!changed.length} onClick={() => setOverrides({})}>
                恢复知识库设置
              </Button>
            </div>
          )}
          {has(kb, 'wiki.read') && (!native || kb.config.wiki.enabled) ? (
            <Switch checked={includeWiki} onChange={setIncludeWiki} label="包含 Wiki 页面" />
          ) : null}
          {native ? (
            <RetrievalSection value={params} onChange={(v) => setOverrides((cur) => ({ ...cur, ...v }))} rerankModel={meta?.models.rerank} />
          ) : optionsSchema ? (
            <SchemaForm schema={optionsSchema} value={optionValues} onChange={setOptionEdits} />
          ) : (
            <div className="field-hint">检索参数由 {kb.backend?.label ?? '知识后端'} 自己的设置决定。</div>
          )}
        </div>
      ) : null}

      {trace ? (
        <div className="trace">
          {COUNT_LABEL.filter(([k]) => trace.counts[k] !== undefined && (trace.counts[k] > 0 || ['fused', 'returned'].includes(k))).map(([k, label]) => (
            <span key={k}>
              {label} <b className="num">{trace.counts[k]}</b>
            </span>
          ))}
          <span>{trace.reranked ? '已重排' : '未重排'}</span>
          {trace.variants.length ? <span title={trace.variants.join(' / ')}>扩展查询 {trace.variants.length} 个</span> : null}
          <span>耗时 {Math.round(trace.elapsed_ms)} ms</span>
        </div>
      ) : null}

      {hits === null ? (
        <Empty icon={<Search size={32} />} title="在这里试试检索" hint="Swarm 的 agent 通过 MCP、DA 通过工具接口调用的是同一套检索。" />
      ) : hits.length === 0 ? (
        <Empty title="没有检索到相关内容" hint="可以在「参数」里调低阈值或打开查询扩展再试。" />
      ) : (
        hits.map((hit, index) => {
          const wiki = hit.chunk_type === 'wiki_page';
          const where = locatorText(hit.locators);
          return (
            <div className={`hit ${wiki ? 'hit-wiki' : ''}`} key={hit.chunk_id}>
              <div className="hit-head">
                <span className="hit-rank">#{index + 1}</span>
                {wiki ? <BookOpen size={14} className="muted" /> : null}
                <span className="hit-title" onClick={() => (wiki ? setOpenWiki(hit.wiki_slug ?? null) : setOpenDoc({ id: hit.doc_id }))}>
                  {hit.title}
                </span>
                <Badge tone="info" title="排序分">
                  {hit.score.toFixed(3)}
                </Badge>
                {(hit.matched_by ?? []).map((m) => (
                  <Badge key={m}>{MATCH_LABEL[m] ?? m}</Badge>
                ))}
                {!wiki && hit.anchor_id ? (
                  <button className="link-btn hit-locate" onClick={() => void locateHit(hit)}>
                    <Crosshair size={12} /> 定位
                  </button>
                ) : null}
              </div>
              <div className="hit-where">
                {wiki
                  ? [hit.context_header, `整理自 ${hit.wiki_sources?.length ?? 0} 篇文档`].join(' · ')
                  : [
                      hit.context_header,
                      where,
                      hit.line_start != null ? `第 ${hit.line_start} 行` : '',
                      hit.merged_chunk_ids?.length ? `合并了 ${hit.merged_chunk_ids.length} 个分块` : '',
                    ]
                      .filter(Boolean)
                      .join(' · ')}
              </div>
              {hit.scores ? (
                <div className="score-row">
                  <span>相关度 {fmt(hit.relevance)}</span>
                  <span>向量 {fmt(hit.scores.vector)}</span>
                  <span>关键词 {fmt(hit.scores.keyword)}</span>
                  <span>重排 {fmt(hit.scores.rerank)}</span>
                </div>
              ) : null}
              {hit.matched_question ? <div className="hit-question">命中问题：{hit.matched_question}</div> : null}
              <Content hit={hit} />
            </div>
          );
        })
      )}
      {openDoc ? (
        <DocDrawer docId={openDoc.id} focus={openDoc.focus} features={kb.features} onClose={() => setOpenDoc(null)} onChanged={() => undefined} />
      ) : null}
      {openWiki ? (
        <WikiPageDrawer kbId={kb.id} slug={openWiki} canEdit={has(kb, 'wiki.write')} features={kb.features} onClose={() => setOpenWiki(null)} />
      ) : null}
    </div>
  );
}
