import { Plus, RotateCcw, X } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import type { ConfigMeta, ConfigSection, KbConfig, RetrievalConfig } from './types';
import { Field, NumberField, Switch } from './ui';

export type ConfigPatch = <S extends ConfigSection>(section: S, values: Partial<KbConfig[S]>) => void;

export const FILE_GROUPS: { label: string; types: string[] }[] = [
  { label: 'PDF', types: ['pdf'] },
  { label: 'Word', types: ['docx', 'doc'] },
  { label: 'PPT', types: ['pptx', 'ppt'] },
  { label: 'Excel', types: ['xlsx', 'xls'] },
  { label: 'CSV / TSV', types: ['csv', 'tsv'] },
  { label: 'Markdown', types: ['md', 'markdown'] },
  { label: 'TXT', types: ['txt'] },
  { label: 'HTML', types: ['html', 'htm', 'mhtml'] },
  { label: 'EPUB', types: ['epub'] },
];

const GRANULARITY: Record<string, string> = {
  focused: '聚焦：只抽文档的核心主题，每篇 3–7 项，索引最干净',
  standard: '标准：核心主题加有实质论述的条目（默认）',
  exhaustive: '详尽：凡是有名字的设备、技术、标准都抽，适合做术语库',
};

export function Group({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="cfg-group">
      <h4 className="cfg-group-title">{title}</h4>
      {hint ? <div className="field-hint" style={{ marginTop: -6, marginBottom: 10 }}>{hint}</div> : null}
      {children}
    </section>
  );
}

function ModelSelect({
  label,
  hint,
  value,
  meta,
  onChange,
  fallback,
}: {
  label: string;
  hint?: string;
  value: string;
  meta: ConfigMeta | null;
  onChange: (value: string) => void;
  fallback?: string;
}) {
  const models = meta?.models.chat ?? [];
  const options = value && !models.includes(value) ? [...models, value] : models;
  return (
    <Field label={label} hint={hint}>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">默认（{fallback || meta?.models.default_chat || '服务默认模型'}）</option>
        {options.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
    </Field>
  );
}

function TextArea({
  label,
  hint,
  value,
  onChange,
  placeholder,
  max = 4000,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  max?: number;
}) {
  return (
    <Field label={label} hint={hint}>
      <textarea
        className="textarea"
        style={{ minHeight: 72 }}
        maxLength={max}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </Field>
  );
}

const showSep = (s: string) => s.replace(/\n/g, '\\n').replace(/\t/g, '\\t').replace(/ /g, '␣');
const parseSep = (s: string) => s.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/␣/g, ' ');

function Separators({ value, defaults, onChange }: { value: string[]; defaults: string[]; onChange: (v: string[]) => void }) {
  const [draft, setDraft] = useState('');
  const add = () => {
    const sep = parseSep(draft);
    if (sep && !value.includes(sep)) onChange([...value, sep]);
    setDraft('');
  };
  return (
    <Field label="分隔符（按优先级）" hint="超长段落按这个顺序依次尝试切开；\n 表示换行，␣ 表示空格。">
      <div className="chips">
        {value.map((sep, i) => (
          <span className="chip" key={`${sep}-${i}`}>
            <code>{showSep(sep)}</code>
            <button className="icon-btn" aria-label="移除" onClick={() => onChange(value.filter((_, j) => j !== i))}>
              <X size={11} />
            </button>
          </span>
        ))}
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        <input className="input" placeholder="新增分隔符，如 \n\n 或 ；" value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} />
        <span style={{ flex: 'none', display: 'flex', gap: 6 }}>
          <button className="btn btn-sm btn-secondary" onClick={add} disabled={!draft}>
            <Plus size={12} />
            <span>添加</span>
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => onChange(defaults)}>
            <RotateCcw size={12} />
            <span>恢复默认</span>
          </button>
        </span>
      </div>
    </Field>
  );
}

export function ParsingSection({ config, meta, patch, setLanguage }: { config: KbConfig; meta: ConfigMeta | null; patch: ConfigPatch; setLanguage: (v: string) => void }) {
  const c = config.chunking;
  const rules = config.parsing.engine_rules;
  const engines = (meta?.parser_engines ?? []).filter((e) => e.available);
  const setRule = (types: string[], engine: string) => {
    const next = { ...rules };
    for (const t of types) {
      if (engine === 'auto') delete next[t];
      else next[t] = engine;
    }
    patch('parsing', { engine_rules: next });
  };
  return (
    <>
      <Group title="索引方式">
        <Switch
          checked={config.indexing.rag_enabled}
          onChange={(v) => patch('indexing', { rag_enabled: v })}
          label="RAG 检索"
          hint="切分、向量化并建 BM25 索引，agent 才能检索到原文。关闭后知识库只用来生成 Wiki（分块仍保留作引用），不再向量化。"
        />
        <Field label="生成内容的语言" hint="摘要、生成问题、Wiki 页面和知识库描述都用这个语言写。">
          <input className="input" list="kb-languages" value={config.language} maxLength={40} onChange={(e) => setLanguage(e.target.value)} />
          <datalist id="kb-languages">
            <option value="中文" />
            <option value="English" />
          </datalist>
        </Field>
      </Group>
      <Group title="分块" hint="修改后只影响之后上传或重新解析的文档；可以在下方「应用到已有文档」里全部重新解析。">
        <Field label="分块策略">
          <select className="select" value={c.strategy} onChange={(e) => patch('chunking', { strategy: e.target.value as 'heading' | 'separator' })}>
            <option value="heading">按标题结构（默认）：不跨标题，表格和代码块保持完整</option>
            <option value="separator">按分隔符：忽略文档结构，只按分隔符和长度切</option>
          </select>
        </Field>
        <div className="row">
          <NumberField label="分块大小（字符）" hint="100–4000，默认 512。" min={100} max={4000} step={50} value={c.chunk_size} onChange={(v) => patch('chunking', { chunk_size: v })} />
          <NumberField label="分块重叠（字符）" hint={`0 到分块大小的一半（${Math.floor(c.chunk_size / 2)}）。`} min={0} max={Math.floor(c.chunk_size / 2)} step={10} value={c.chunk_overlap} onChange={(v) => patch('chunking', { chunk_overlap: v })} />
        </div>
        <Separators value={c.separators} defaults={meta?.defaults.chunking.separators ?? c.separators} onChange={(v) => patch('chunking', { separators: v })} />
      </Group>
      <Group title="解析引擎" hint="按文件类型选 docreader 引擎；「自动」走 kb-service 默认路由（文本和表格本地解析，其余用 builtin，带页码定位）。">
        <table className="table cfg-table">
          <tbody>
            {FILE_GROUPS.map((g) => {
              const options = engines.filter((e) => g.types.some((t) => e.file_types.includes(t)));
              const current = rules[g.types[0]] ?? 'auto';
              return (
                <tr key={g.label}>
                  <td style={{ width: 120 }}>{g.label}</td>
                  <td>
                    <select className="select" value={current} onChange={(e) => setRule(g.types, e.target.value)}>
                      <option value="auto">自动</option>
                      {options.map((e) => (
                        <option key={e.name} value={e.name}>
                          {e.name} · {e.description}
                        </option>
                      ))}
                      {current !== 'auto' && !options.some((e) => e.name === current) ? <option value={current}>{current}（不可用）</option> : null}
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Group>
    </>
  );
}

export function EnrichSection({ config, meta, patch }: { config: KbConfig; meta: ConfigMeta | null; patch: ConfigPatch }) {
  const s = config.summary;
  const q = config.questions;
  const p = config.profile;
  return (
    <>
      <Group title="文档摘要" hint="每篇文档解析完后生成一段摘要（L0），agent 浏览文档清单和 Wiki 生成都会用到。">
        <Switch checked={s.enabled} onChange={(v) => patch('summary', { enabled: v })} label="生成文档摘要" />
        {s.enabled ? (
          <>
            <div className="row">
              <ModelSelect label="摘要模型" value={s.model} meta={meta} onChange={(v) => patch('summary', { model: v })} />
              <NumberField label="送入模型的正文长度（字符）" hint="1000–30000，默认 6000。" min={1000} max={30000} step={1000} value={s.input_chars} onChange={(v) => patch('summary', { input_chars: v })} />
            </div>
            <TextArea label="摘要要求" placeholder="例如：说明设备型号、适用工况和关键参数…" value={s.instructions} onChange={(v) => patch('summary', { instructions: v })} />
            <TextArea label="表格元数据生成要求" hint="只对 CSV / Excel 生效：补充业务背景和字段含义，帮助检索理解表格。" placeholder="例如：这是设备巡检记录表，温度单位为 ℃，status 字段 1 表示正常…" value={s.table_instructions} onChange={(v) => patch('summary', { table_instructions: v })} />
          </>
        ) : null}
      </Group>
      <Group title="AI 问题生成" hint="解析后让大模型为每个分块写几个它能回答的问题，问题单独建向量和关键词索引，命中后返回原分块，能明显提高召回。会增加处理耗时和模型调用。">
        <Switch checked={q.enabled} disabled={!config.indexing.rag_enabled} onChange={(v) => patch('questions', { enabled: v })} label="为分块生成问题" hint={!config.indexing.rag_enabled ? '需要先开启 RAG 检索。' : undefined} />
        {q.enabled ? (
          <>
            <div className="row">
              <NumberField label="每个分块的问题数" hint="1–10，默认 3。" min={1} max={10} value={q.count} onChange={(v) => patch('questions', { count: v })} />
              <ModelSelect label="问题生成模型" value={q.model} meta={meta} onChange={(v) => patch('questions', { model: v })} />
            </div>
            <TextArea label="问题生成要求" hint="指定提问的人群、场景和说法。" placeholder="例如：站在现场运维人员角度，用口语化的方式提问…" value={q.instructions} onChange={(v) => patch('questions', { instructions: v })} />
          </>
        ) : null}
      </Group>
      <Group title="自动生成知识库描述" hint="文档变化后汇总各文档摘要，生成一段知识库描述；可以一键用作知识库说明，agent 据此判断要不要检索这个库。">
        <Switch checked={p.enabled} onChange={(v) => patch('profile', { enabled: v })} label="自动生成描述" />
        {p.enabled ? (
          <>
            <ModelSelect label="生成模型" value={p.model} meta={meta} onChange={(v) => patch('profile', { model: v })} />
            <TextArea label="描述生成要求" max={2000} placeholder="例如：说明适用的装置和岗位…" value={p.instructions} onChange={(v) => patch('profile', { instructions: v })} />
          </>
        ) : null}
      </Group>
    </>
  );
}

export function WikiSection({ config, meta, patch }: { config: KbConfig; meta: ConfigMeta | null; patch: ConfigPatch }) {
  const w = config.wiki;
  return (
    <>
      <Group title="Wiki 知识库" hint="大模型把文档提炼成相互链接的 Wiki：每篇文档一个摘要页，抽出的实体（设备、组织、产品…）和概念（方法、流程、标准…）各一页，多篇文档讲到同一个东西会合并到同一页。">
        <Switch checked={w.enabled} onChange={(v) => patch('wiki', { enabled: v })} label="开启 Wiki" hint="开启后新文档会自动生成 Wiki；已有文档到 Wiki 页签里点「重建」。" />
        {w.enabled ? (
          <>
            <div className="row">
              <ModelSelect label="Wiki 合成模型" value={w.model} meta={meta} onChange={(v) => patch('wiki', { model: v })} />
              <Field label="提取粒度">
                <select className="select" value={w.granularity} onChange={(e) => patch('wiki', { granularity: e.target.value as KbConfig['wiki']['granularity'] })}>
                  {Object.entries(GRANULARITY).map(([k, label]) => (
                    <option key={k} value={k}>
                      {label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <TextArea label="Wiki 提取重点" hint="告诉模型这个领域里哪些东西值得单独成页。" placeholder="例如：重点提取设备位号、工艺单元、故障现象和处置规程…" value={w.extraction_instructions} onChange={(v) => patch('wiki', { extraction_instructions: v })} />
            <TextArea label="Wiki 内容生成要求" hint="页面的语气、结构和侧重点；事实约束优先于这里的要求。" placeholder="例如：先写结论再写依据，参数用表格列出…" value={w.content_instructions} onChange={(v) => patch('wiki', { content_instructions: v })} />
            <NumberField label="每篇文档最多生成的页面数" hint="0 表示不限制。" min={0} max={200} value={w.max_pages_per_doc} onChange={(v) => patch('wiki', { max_pages_per_doc: v })} />
          </>
        ) : null}
      </Group>
      {w.enabled ? (
        <Group title="Wiki 参与检索" hint="Wiki 页面也建了向量和关键词索引，可以和原文片段一起参与检索和重排。">
          <Switch checked={w.include_in_search} onChange={(v) => patch('wiki', { include_in_search: v })} label="检索结果里包含 Wiki 页面" />
          {w.include_in_search ? (
            <div className="row">
              <NumberField label="最多返回几个 Wiki 页面" hint="0–20，默认 2。" min={0} max={20} value={w.max_search_results} onChange={(v) => patch('wiki', { max_search_results: v })} />
              <NumberField label="Wiki 加权系数" hint="1–3，默认 1.3：Wiki 是预先综合过的知识，同分时优先。" min={1} max={3} step={0.1} value={w.boost} onChange={(v) => patch('wiki', { boost: v })} />
            </div>
          ) : null}
        </Group>
      ) : null}
    </>
  );
}

export function RetrievalSection({ value, onChange, rerankModel }: { value: RetrievalConfig; onChange: (v: Partial<RetrievalConfig>) => void; rerankModel?: string | null }) {
  const r = value;
  return (
    <>
      <Group title="召回" hint="向量和关键词两路并行召回，阈值按各自知识库的设置过滤。">
        <div className="cfg-pair">
          <Switch checked={r.vector_enabled} onChange={(v) => onChange({ vector_enabled: v })} label="向量检索" />
          <div className="row">
            <NumberField label="向量召回数" min={1} max={200} value={r.vector_top_k} disabled={!r.vector_enabled} onChange={(v) => onChange({ vector_top_k: v })} />
            <NumberField label="向量相似度阈值" hint="0–1，余弦相似度，默认 0.2。" min={0} max={1} step={0.05} value={r.vector_threshold} disabled={!r.vector_enabled} onChange={(v) => onChange({ vector_threshold: v })} />
          </div>
        </div>
        <div className="cfg-pair">
          <Switch checked={r.keyword_enabled} onChange={(v) => onChange({ keyword_enabled: v })} label="关键词检索（BM25，中文分词）" />
          <div className="row">
            <NumberField label="关键词召回数" min={1} max={200} value={r.keyword_top_k} disabled={!r.keyword_enabled} onChange={(v) => onChange({ keyword_top_k: v })} />
            <NumberField label="关键词阈值" hint="0–1，按本次最高分归一化，默认 0（不过滤）。" min={0} max={1} step={0.05} value={r.keyword_threshold} disabled={!r.keyword_enabled} onChange={(v) => onChange({ keyword_threshold: v })} />
          </div>
        </div>
        <Switch checked={r.use_questions} onChange={(v) => onChange({ use_questions: v })} label="匹配生成的问题" hint="开启了 AI 问题生成时，问题命中也算该分块命中。" />
        <Switch checked={r.query_expansion} onChange={(v) => onChange({ query_expansion: v })} label="查询扩展" hint="召回不足时，去掉疑问词、按标点拆句再做几次关键词检索（不调用模型）。" />
      </Group>
      <Group title="融合" hint="两路结果用加权 RRF 合并：分数 = Σ 权重 /（k + 名次）。">
        <div className="row">
          <NumberField label="RRF k" min={1} max={1000} value={r.rrf_k} onChange={(v) => onChange({ rrf_k: v })} />
          <NumberField label="向量权重" min={0} max={5} step={0.1} value={r.vector_weight} onChange={(v) => onChange({ vector_weight: v })} />
          <NumberField label="关键词权重" min={0} max={5} step={0.1} value={r.keyword_weight} onChange={(v) => onChange({ keyword_weight: v })} />
        </div>
      </Group>
      <Group title="重排" hint={rerankModel ? `重排模型：${rerankModel}` : '服务没有配置重排模型，重排不会生效。'}>
        <Switch checked={r.rerank_enabled} onChange={(v) => onChange({ rerank_enabled: v })} label="模型重排" />
        <div className="row">
          <NumberField label="送去重排的候选数" min={1} max={200} value={r.rerank_candidates} disabled={!r.rerank_enabled} onChange={(v) => onChange({ rerank_candidates: v })} />
          <NumberField
            label="重排阈值"
            hint="低于阈值的丢弃；都不过时阈值大于 0.3 会降到 0.7 倍再试一次，仍为空则保留最好的一条（≥0.15）。"
            min={-10}
            max={10}
            step={0.05}
            value={r.rerank_threshold}
            disabled={!r.rerank_enabled}
            onChange={(v) => onChange({ rerank_threshold: v })}
          />
        </div>
        <Switch checked={r.composite_score} disabled={!r.rerank_enabled} onChange={(v) => onChange({ composite_score: v })} label="复合打分" hint="最终分 = 0.6 × 重排分 + 0.3 × 召回分 + 0.1。关闭则只用重排分。" />
      </Group>
      <Group title="结果整理">
        <div className="cfg-pair">
          <Switch checked={r.mmr_enabled} onChange={(v) => onChange({ mmr_enabled: v })} label="MMR 多样性" hint="挑结果时扣掉和已选结果重复的部分，避免几条都在说同一件事。" />
          <NumberField label="相关性权重 λ" hint="0–1，默认 0.7；越小越强调多样性。" min={0} max={1} step={0.05} value={r.mmr_lambda} disabled={!r.mmr_enabled} onChange={(v) => onChange({ mmr_lambda: v })} />
        </div>
        <Switch checked={r.merge_adjacent} onChange={(v) => onChange({ merge_adjacent: v })} label="合并相邻分块" hint="同一文档里前后相连或重叠的命中合成一条。" />
        <div className="cfg-pair">
          <Switch checked={r.expand_enabled} disabled={r.parent_child} onChange={(v) => onChange({ expand_enabled: v })} label="短片段补上下文" hint="命中片段太短时，向前后各取一部分原文补齐。" />
          <div className="row">
            <NumberField label="少于多少字时补" min={0} max={4000} step={50} value={r.expand_min_chars} disabled={!r.expand_enabled || r.parent_child} onChange={(v) => onChange({ expand_min_chars: v })} />
            <NumberField label="补齐到多少字" min={100} max={8000} step={50} value={r.expand_max_chars} disabled={!r.expand_enabled || r.parent_child} onChange={(v) => onChange({ expand_max_chars: v })} />
          </div>
        </div>
        <div className="cfg-pair">
          <Switch checked={r.parent_child} onChange={(v) => onChange({ parent_child: v })} label="父子分块" hint="用小分块匹配，返回它所在的整个章节（不超过父块大小）。开启后不再做短片段补齐。" />
          <NumberField label="父块大小（字符）" min={512} max={8192} step={256} value={r.parent_chunk_size} disabled={!r.parent_child} onChange={(v) => onChange({ parent_chunk_size: v })} />
        </div>
      </Group>
    </>
  );
}
