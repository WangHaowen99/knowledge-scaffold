import { useEffect, useMemo, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { store, actions, materialPurposeLabel } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { docSample, materials } from '../lib/mock/fixtures.js';
import Icon from '../ui/Icon.jsx';
import { downloadMaterial, downloadTermTemplate, getPackageFile } from '../lib/api.js';
import { readableEvidenceAnchor } from '../lib/evidence.js';
import { materialEvidenceSelection, materialMarkdown, materialReferences, rehypeMaterialHighlights } from '../lib/materialDocument.js';

const selAnchor = (s) => s.activeAnchor;
const selMat = (s) => s.selectedMaterial;
const selDemo = (s) => s.demo;
const selMaterials = (s) => s.materials;
const selMaterialContents = (s) => s.materialContents;
const selMaterialReferrers = (s) => s.materialReferrers;

const SHEET_DISPOSITIONS = [
  { value: 'object', label: '实体数据', semanticRole: 'schema' },
  { value: 'relationship', label: '关系数据', semanticRole: 'schema' },
  { value: 'derived', label: '派生汇总', semanticRole: 'schema' },
  { value: 'reference', label: '业务依据', semanticRole: 'evidence' },
  { value: 'ignore', label: '忽略', semanticRole: 'ignore' },
];
const RELATIONSHIP_TEMPLATE_SHEET_NAME = '主外键映射';
const RELATIONSHIP_TEMPLATE_HEADERS = ['table_name', 'column_name', 'constraint_type', 'referenced_table', 'referenced_column'];

function relationshipTemplateStatus(table, tables = []) {
  if (String(table.name || '').trim() !== RELATIONSHIP_TEMPLATE_SHEET_NAME) return { matched: false, text: '模板未匹配' };
  const headers = (table.fields || []).map((field) => String(field.name || '').trim());
  const headerMatched = headers.length === RELATIONSHIP_TEMPLATE_HEADERS.length
    && RELATIONSHIP_TEMPLATE_HEADERS.every((header, index) => headers[index] === header);
  if (!headerMatched) return { matched: false, text: '模板未匹配' };
  const names = new Set((tables || []).map((item) => String(item.name || '').trim()).filter(Boolean));
  let foreignKeyCount = 0;
  for (const row of table.rows || []) {
    const values = RELATIONSHIP_TEMPLATE_HEADERS.map((_, index) => String(row?.[index] || '').trim());
    const [tableName, columnName, constraintType, referencedTable, referencedColumn] = values;
    if (!values.some(Boolean)) continue;
    if (!tableName || !columnName || (names.size && !names.has(tableName))) return { matched: false, text: '模板未匹配' };
    if (constraintType === 'PRIMARY_KEY') {
      if (referencedTable || referencedColumn) return { matched: false, text: '模板未匹配' };
    } else if (constraintType === 'FOREIGN_KEY') {
      if (!referencedTable || !referencedColumn || (names.size && !names.has(referencedTable))) return { matched: false, text: '模板未匹配' };
      foreignKeyCount += 1;
    } else {
      return { matched: false, text: '模板未匹配' };
    }
  }
  return { matched: true, text: foreignKeyCount ? `模板已匹配：${foreignKeyCount} 条外键关系` : '模板已匹配：暂无外键关系' };
}

function decisionForSheet(sheet, disposition = 'reference', reason = '') {
  const option = SHEET_DISPOSITIONS.find((item) => item.value === disposition)
    || SHEET_DISPOSITIONS[3];
  return {
    sheet,
    semanticRole: option.semanticRole,
    importDisposition: option.value,
    ...(reason ? { reason } : {}),
  };
}

function purposeReviewForm(material) {
  const review = material.purposeReview;
  const source = review?.status === 'confirmed' ? review.sheetDecisions : review?.sheetSuggestions;
  const bySheet = new Map((source || []).map((item) => [item.sheet, item]));
  return {
    purpose: review?.confirmedPurpose || review?.suggestedPurpose || material.purpose || 'evidence',
    decisions: Object.fromEntries(material.tables.map((table) => {
      const decision = bySheet.get(table.name);
      return [table.name, decisionForSheet(
        table.name,
        decision?.importDisposition || (material.purpose === 'schema' ? 'object' : 'reference'),
        decision?.reason,
      )];
    })),
  };
}

/* 双向反查是资料界面的全部理由。
   草图里反查面板占右栏，但右栏在正式版恒为对话流（「只有一条流」），
   所以反查改成中栏内的第二列——阅读上下文不丢，右栏不被抢占。 */
export default function DocView() {
  const anchor = useStore(store, selAnchor);
  const matId = useStore(store, selMat);
  const demo = useStore(store, selDemo);
  const realMaterials = useStore(store, selMaterials);
  const materialContents = useStore(store, selMaterialContents);
  const materialReferrers = useStore(store, selMaterialReferrers);
  if (!demo) {
    const summary = realMaterials.find((item) => item.id === matId);
    const material = materialContents[matId];
    if (summary?.parseStatus === 'failed') return <Unparsed mat={{ name: summary.filename }} error={summary.parseError} />;
    if (!material) return <div className="empty">正在读取资料…</div>;
    return <RealDocument key={`${matId}:${material.revisionNo}`} material={material} referrers={materialReferrers[matId] || []}
      location={typeof anchor === 'object' ? anchor : null} />;
  }
  const mat = materials.find((m) => m.id === matId) || materials[0];

  if (mat.st === 'stale') return <Unparsed mat={mat} />;

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 268px', gap: 'var(--s-5)', alignItems: 'start' }}>
      <div className="wb-doc">
        {docSample.blocks.map((b, i) => {
          if (b.h) return <h5 key={i}>{b.h}</h5>;
          return (
            <p key={i}>
              {b.p.map(([text, mark], j) => {
                if (mark !== 'hl') return <span key={j}>{text}</span>;
                hlIndex += 1;
                const idx = hlIndex;
                return (
                  <mark key={j} className={`wb-hl${anchor === idx ? ' on' : ''}`}
                    onClick={() => actions.setAnchor(idx)} title="查看引用方">{text}</mark>
                );
              })}
            </p>
          );
        })}
        <p className="t-sm muted" style={{ marginTop: 'var(--s-5)', marginBottom: 0 }}>
          黄色为被知识条目引用的段落，点击查看引用方。选中任意段落可手动建立引用。
        </p>
      </div>

      <aside style={{ position: 'sticky', top: 0, display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
        <div className="card">
          <div className="card-h">这段被谁引用<span className="spacer" />{docSample.referrers.length} 条</div>
          <div className="wb-backref">
            {docSample.referrers.map((r) => (
              <a key={r.path} onClick={() => actions.selectPath(r.path)} title={r.path}>
                <RefIcon path={r.path} />
                <div>
                  <b>{r.path.split('/').pop()}</b>
                  <span>{r.why}</span>
                </div>
              </a>
            ))}
          </div>
        </div>

        <div className="card card-stale">
          <div className="card-h"><Icon name="alert" size={12} />和现行知识不完全一致</div>
          <div className="card-b t-sm">
            原文只说「扣除已占用」，没提在途退货；<code className="k">rules/安全库存判断.md</code> 多扣了一项，
            依据来自你 2026-08-22 的裁决。
            <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
              <button className="btn btn-sm">看那次裁决</button>
              <button className="btn btn-sm">让助手复核</button>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-h">这份资料</div>
          <div className="card-b t-sm">
            <div className="muted">v{mat.v} · 上传于 2026-08-14 · 被 {mat.refs} 条知识引用</div>
            <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
              <button className="btn btn-sm"><Icon name="upload" size={12} />新版本</button>
              <button className="btn btn-sm"><Icon name="search" size={12} />搜索</button>
              <button className="btn btn-sm btn-danger">删除</button>
            </div>
            <div style={{ color: 'var(--c-stale)', marginTop: 'var(--s-3)' }}>
              删除会让 {mat.refs} 条知识失去证据，需逐条重新指认或标为待补。
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
}

function RefIcon({ path }) {
  const ext = (path || '').split('.').pop();
  const name = (() => { if (ext === 'mmd') return 'layers'; return ext === 'xlsx' || ext === 'csv' ? 'sheet' : 'file'; })();
  return <Icon name={name} size={15} className="ref-icon" />;
}

function Unparsed({ mat, error }) {
  return (
    <div className="card card-stale">
      <div className="card-h"><Icon name="alert" size={12} />无法解析</div>
      <div className="card-b">
        <div className="t-md">{mat.name} 无法提取可读取的文字。{error ? ` ${error}` : ''}</div>
        <div className="btn-row" style={{ marginTop: 'var(--s-4)' }}>
          <button className="btn">换一个可复制文字的版本</button>
          <button className="btn btn-p">我口头说明内容</button>
          <button className="btn btn-danger">移除</button>
        </div>
      </div>
    </div>
  );
}

function RealDocument({ material, referrers, location }) {
  const revisionInput = useRef(null);
  const body = useRef(null);
  const request = useRef(0);
  const [selected, setSelected] = useState(null);
  const [loadingPath, setLoadingPath] = useState('');
  const [referenceError, setReferenceError] = useState('');
  const selection = useMemo(() => materialEvidenceSelection(material,
    selected?.locations || (location ? [location] : [])), [material, selected, location]);
  const paragraphs = documentParagraphs(material.textContent);
  useEffect(() => () => { request.current += 1; }, []);
  useEffect(() => {
    request.current += 1;
    setSelected(null);
    setLoadingPath('');
    setReferenceError('');
  }, [location]);
  useEffect(() => {
    body.current?.querySelector('.wb-evidence-mark, .wb-evidence-table')
      ?.scrollIntoView({ block: 'center', inline: 'nearest' });
  }, [selection]);
  let documentContent;
  if (paragraphs.length === 0) {
    documentContent = <div className="empty">这份资料没有可显示的正文{material.tables.length ? `，包含 ${material.tables.length} 张数据表` : ''}</div>;
  } else if (isMarkdownMaterial(material.filename)) {
    documentContent = <MaterialMarkdown content={material.textContent} ranges={selection.ranges} />;
  } else {
    documentContent = paragraphs.map((paragraph) => (
      <DocumentParagraph key={paragraph.start} paragraph={paragraph} ranges={selection.ranges} />
    ));
  }
  const locateReferrer = async (item) => {
    const current = ++request.current;
    setLoadingPath(item.path);
    setReferenceError('');
    setSelected({ path: item.path, locations: [] });
    try {
      // The referrer summary is truncated; read the full file to find every cited location.
      const result = await getPackageFile(store.get().projectId, item.path);
      if (current !== request.current) return;
      const locations = materialReferences(result.content, material);
      setSelected({ path: item.path, locations });
      if (!locations.length) setReferenceError('这条知识只关联了整份资料，未记录可定位的原文片段，请结合全文核对。');
    } catch (error) {
      if (current === request.current) setReferenceError(error instanceof Error ? error.message : '引用内容读取失败，请点击条目重试。');
    } finally {
      if (current === request.current) setLoadingPath('');
    }
  };
  const download = async () => {
    const response = await downloadMaterial(store.get().projectId, material.id);
    const blob = await response.blob();
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = material.filename;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(href), 1_000);
  };
  const downloadTemplate = async () => {
    const response = await downloadTermTemplate(store.get().projectId);
    const blob = await response.blob();
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = 'term-table-template.xlsx';
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(href), 1_000);
  };
  return (
    <div className="wb-doc-layout">
    <div className="wb-doc" ref={body}>
      <div className="t-sm muted" style={{ marginBottom: 'var(--s-4)' }}>
        {material.filename} · v{material.revisionNo} · {material.textContent.length} 字符
      </div>
      {(location || selected) && (
        <div className="wb-evidence-location" role="status">
          <Icon name="search" size={13} />
          <span>{loadingPath ? '正在定位被引用的原文…' : <>
            {selected?.path && <b>{selected.path.split('/').pop()}： </b>}
            {(selection.ranges.length > 0 || selection.tableNames.length > 0) && '已高亮引用内容，可结合上下文核对。'}
            {selection.wholeDocument && '该引用指向整份资料，未指定原文片段。'}
            {selection.missing.length > 0 && `未找到引用位置「${selection.missing.map(readableEvidenceAnchor).join('、')}」，请结合全文核对。`}
            {referenceError}
          </>}</span>
          <button type="button" className="btn btn-sm" onClick={() => {
            request.current += 1;
            setSelected(null);
            setLoadingPath('');
            setReferenceError('');
            actions.setAnchor(null);
          }}>清除定位</button>
        </div>
      )}
      {documentContent}
      {material.tables.map((table) => (
        <div className={`card${selection.tableNames.includes(table.name) ? ' wb-evidence-table' : ''}`} key={table.name} style={{ marginTop: 'var(--s-4)' }}>
          <div className="card-h">{table.name}<span className="spacer" />{table.rowCount} 行</div>
          <div className="card-b t-sm">{table.fields.map((field) => field.name).join(' · ')}</div>
        </div>
      ))}
    </div>
    <aside className="card wb-doc-refs">
      <div className="card-h">这份资料被谁引用<span className="spacer" />{referrers.length} 条</div>
      <div className="wb-backref">
        {referrers.map((item) => (
          <div key={item.path} className={`wb-referrer${selected?.path === item.path ? ' on' : ''}`}>
            <button type="button" className="wb-referrer-locate" onClick={() => void locateReferrer(item)}
              aria-pressed={selected?.path === item.path} aria-busy={loadingPath === item.path}
              title={`定位「${item.path}」引用的原文`}>
              <RefIcon path={item.path} />
              <span><b>{item.path.split('/').pop()}</b><small>{loadingPath === item.path ? '正在定位…' : '点击定位并高亮原文'}</small></span>
            </button>
            <div className="wb-referrer-actions">
              <span>{item.occurrences ? `${item.occurrences} 处引用` : `第 ${item.line} 行`}</span>
              <button type="button" onClick={() => actions.selectPath(item.path)}>打开知识条目 →</button>
            </div>
          </div>
        ))}
        {!referrers.length && <div className="empty t-sm">当前知识包还没有引用这份资料</div>}
      </div>
      <div className="card-b" style={{ borderTop: '1px solid var(--c-line-soft)' }}>
        <div className="wb-doc-directives">
          {!material.purposeReview && <label><span>用途</span><select className="input" value={material.purpose || 'auto'} aria-label={`${material.filename}的资料用途`}
            onChange={(event) => void actions.setMaterialDirectives(material.id, { purpose: event.target.value })}>
            {['auto', 'goal', 'schema', 'term', 'evidence'].map((value) => <option key={value} value={value}
              disabled={['schema', 'term'].includes(value) && !['excel', 'csv'].includes(material.kind)}>{materialPurposeLabel(value)}</option>)}
          </select></label>}
          {material.purpose === 'term' && <button className="btn btn-sm" type="button" onClick={() => void downloadTemplate()}>
            <Icon name="download" size={12} />下载模板
          </button>}
          <label className="wb-material-reading"><input type="checkbox" checked={material.readingMode === 'full'}
            onChange={(event) => void actions.setMaterialDirectives(material.id, { readingMode: event.target.checked ? 'full' : 'on_demand' })} />
            <span>全文精读</span></label>
        </div>
        {material.purposeReview && <MaterialPurposeReviewPanel material={material} />}
        <div className="btn-row">
          <button className="btn btn-sm" onClick={() => void download()}><Icon name="download" size={12} />下载原文件</button>
          <button className="btn btn-sm" onClick={() => revisionInput.current?.click()}><Icon name="upload" size={12} />上传新版本</button>
        </div>
        <input ref={revisionInput} type="file" hidden
          accept=".md,.txt,.pdf,.doc,.docx,.xls,.xlsx,.csv"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = '';
            if (file) void actions.upload([file], { replacementOf: material.id });
          }} />
      </div>
    </aside>
    </div>
  );
}

function isMarkdownMaterial(filename) {
  return /\.(?:md|markdown)$/iu.test(String(filename || ''));
}

export function MaterialMarkdown({ content, ranges }) {
  const source = materialMarkdown(content);
  const plugins = useMemo(() => [[rehypeMaterialHighlights, { ranges, source }]], [ranges, source]);
  return <article className="wb-markdown wb-material-markdown">
    <Markdown remarkPlugins={[remarkGfm]} rehypePlugins={plugins} components={{
      table: ({ node: _node, children, ...props }) => <div className="wb-material-table"><table {...props}>{children}</table></div>,
      img: ({ node: _node, alt }) => <span className="wb-markdown-image-note">图片：{alt || '未命名图片'}</span>,
    }}>{source}</Markdown>
  </article>;
}

function MaterialPurposeReviewPanel({ material }) {
  const projectMaterials = useStore(store, selMaterials);
  const review = material.purposeReview;
  const initial = purposeReviewForm(material);
  const [purpose, setPurpose] = useState(initial.purpose === 'auto' ? 'evidence' : initial.purpose);
  const [decisions, setDecisions] = useState(initial.decisions);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const next = purposeReviewForm(material);
    setPurpose(next.purpose === 'auto' ? 'evidence' : next.purpose);
    setDecisions(next.decisions);
    setError('');
  }, [material.id, review.updatedAt]);

  const ordered = material.tables.map((table) => decisions[table.name]).filter(Boolean);
  const hasSchemaSheet = ordered.some((item) => item.semanticRole === 'schema');
  const validationError = (() => { if (ordered.length !== material.tables.length) return '仍有 Sheet 未分类'; return hasSchemaSheet && purpose !== 'schema'
      ? '包含实体、关系或汇总 Sheet 时，文件用途必须是数据 Schema'
      : (() => { if (purpose === 'schema' && !hasSchemaSheet) return '数据 Schema 至少需要一个实体、关系或汇总 Sheet'; return ''; })(); })();
  const analysisPending = review.status !== 'confirmed' && review.suggestedBy !== 'agent';
  const reviewable = projectMaterials.filter((item) => item.purposeReview);
  const confirmedCount = reviewable.filter((item) => item.purposeReview.status === 'confirmed').length;
  const tabular = ['excel', 'csv'].includes(material.kind) && material.tables.length > 0;
  const relationshipStatuses = useMemo(() => Object.fromEntries(material.tables.map((table) => [
    table.name,
    relationshipTemplateStatus(table, material.tables),
  ])), [material.tables]);

  const changeDisposition = (sheet, disposition) => {
    setDecisions((current) => ({
      ...current,
      [sheet]: decisionForSheet(sheet, disposition),
    }));
    if (['object', 'relationship', 'derived'].includes(disposition)) setPurpose('schema');
    setError('');
  };
  const confirm = async () => {
    if (validationError || analysisPending) return;
    setSaving(true);
    setError('');
    const result = await actions.confirmMaterialPurposeReview(material.id, purpose, ordered);
    if (!result) setError(store.get().error || '资料用途确认失败');
    setSaving(false);
  };

  return (
    <section className="wb-purpose-review" aria-label="资料用途确认">
      <div className="wb-purpose-review-head">
        <b>{tabular ? '文件及 Sheet 用途' : '资料用途确认'}</b>
        {reviewable.length > 1 && <span className="muted">{confirmedCount}/{reviewable.length}</span>}
        <span className={`pill ${review.status === 'confirmed' ? 'ok' : 'wait'}`}>
          {(() => { if (analysisPending) return '助手轻量分析中'; return review.status === 'confirmed' ? '已确认' : '待确认'; })()}
        </span>
      </div>
      <label className="wb-purpose-file">
        <span>文件用途</span>
        <select className="input" value={purpose} disabled={analysisPending}
          onChange={(event) => { setPurpose(event.target.value); setError(''); }}>
          {['goal', 'schema', 'term', 'evidence'].map((value) => (
            <option key={value} value={value}
              disabled={['schema', 'term'].includes(value) && !tabular}>{materialPurposeLabel(value)}</option>
          ))}
        </select>
      </label>
      {tabular && <div className="t-xs muted">
        关系数据模板：Sheet 名固定为「主外键映射」，表头为 {RELATIONSHIP_TEMPLATE_HEADERS.join('、')}。
      </div>}
      <div className="wb-purpose-sheets">
        {material.tables.map((table) => (
          <label key={table.name} className="wb-purpose-sheet">
            <span title={table.name}>{table.name}</span>
            <select className="input" value={decisions[table.name]?.importDisposition || 'reference'}
              aria-label={`${table.name}的用途`}
              disabled={analysisPending}
              onChange={(event) => changeDisposition(table.name, event.target.value)}>
              {SHEET_DISPOSITIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
            {decisions[table.name]?.reason && <small>{decisions[table.name].reason}</small>}
            {decisions[table.name]?.importDisposition === 'relationship' && (
              <small>{relationshipStatuses[table.name]?.text || '模板未匹配'}</small>
            )}
          </label>
        ))}
      </div>
      {(error || validationError) && <div className="wb-purpose-error" role="alert">{error || validationError}</div>}
      <button className="btn btn-p btn-sm" type="button" disabled={saving || analysisPending || Boolean(validationError)}
        onClick={() => void confirm()}>
        {(() => { if (saving) return '确认中…'; return review.status === 'confirmed' ? '更新确认' : (() => { if (tabular) return '确认文件及全部 Sheet'; return '确认资料用途'; })(); })()}
      </button>
    </section>
  );
}

export function documentParagraphs(content) {
  const text = String(content || '');
  const blocks = [];
  const separator = /\n{2,}/g;
  let start = 0;
  const add = (end) => {
    const raw = text.slice(start, end);
    const first = raw.search(/\S/u);
    if (first < 0) return;
    const body = raw.slice(first).replace(/\s+$/u, '');
    blocks.push({ text: body, start: start + first, end: start + first + body.length });
  };
  for (let match = separator.exec(text); match; match = separator.exec(text)) {
    add(match.index);
    start = match.index + match[0].length;
  }
  add(text.length);
  return blocks;
}

export function evidenceHighlight(paragraph, location) {
  const offset = evidenceLocationOffset(location);
  if (offset === null || offset < paragraph.start || offset > paragraph.end) return null;
  const candidates = [location.anchor, String(location.anchor || '').replace(/^§/u, '')].filter(Boolean);
  for (const candidate of candidates) {
    const index = paragraph.text.indexOf(candidate);
    if (index >= 0) return { start: index, end: index + candidate.length };
  }
  const start = Math.max(0, Math.min(paragraph.text.length - 1, offset - paragraph.start));
  return { start, end: Math.min(paragraph.text.length, start + Math.min(120, paragraph.text.length - start)) };
}

export function evidenceLocationOffset(location) {
  if (!location) return null;
  const explicit = String(location.anchor || '').match(/^offset=(\d+)$/iu)?.[1];
  const value = explicit === undefined ? Number(location.offset) : Number(explicit);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

function DocumentParagraph({ paragraph, ranges }) {
  const hits = ranges.filter((range) => range.start < paragraph.end && range.end > paragraph.start)
    .map((range) => ({ start: Math.max(0, range.start - paragraph.start), end: Math.min(paragraph.text.length, range.end - paragraph.start) }))
    .sort((a, b) => a.start - b.start);
  const children = [];
  let cursor = 0;
  for (const hit of hits) {
    if (hit.end <= cursor) continue;
    if (hit.start > cursor) children.push(paragraph.text.slice(cursor, hit.start));
    const start = Math.max(cursor, hit.start);
    children.push(<mark key={start} className="wb-evidence-mark">{paragraph.text.slice(start, hit.end)}</mark>);
    cursor = hit.end;
  }
  children.push(paragraph.text.slice(cursor));
  return <p className="wb-document-paragraph">{children}</p>;
}
