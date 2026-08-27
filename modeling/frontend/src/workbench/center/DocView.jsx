import { useEffect, useRef } from 'react';
import { store, actions, materialPurposeLabel } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { docSample, materials } from '../lib/mock/fixtures.js';
import Icon from '../ui/Icon.jsx';
import { downloadMaterial, downloadTermTemplate } from '../lib/api.js';
import { readableEvidenceAnchor } from '../lib/evidence.js';

const selAnchor = (s) => s.activeAnchor;
const selMat = (s) => s.selectedMaterial;
const selDemo = (s) => s.demo;
const selMaterials = (s) => s.materials;
const selMaterialContents = (s) => s.materialContents;
const selMaterialReferrers = (s) => s.materialReferrers;

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
    return <RealDocument material={material} referrers={materialReferrers[matId] || []}
      location={typeof anchor === 'object' ? anchor : null} />;
  }
  const mat = materials.find((m) => m.id === matId) || materials[0];

  if (mat.st === 'stale') return <Unparsed mat={mat} />;

  let hlIndex = -1;
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
  const name = ext === 'mmd' ? 'layers' : ext === 'xlsx' || ext === 'csv' ? 'sheet' : 'file';
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
  const paragraphs = documentParagraphs(material.textContent);
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
    <div className="wb-doc">
      <div className="t-sm muted" style={{ marginBottom: 'var(--s-4)' }}>
        {material.filename} · v{material.revisionNo} · {material.textContent.length} 字符
      </div>
      {location && (
        <div className="wb-evidence-location" role="status">
          <Icon name="search" size={13} />
          <span>{location.anchor ? <>已定位到 <b>{readableEvidenceAnchor(location.anchor)}</b> 对应的原文</> : '已定位到引用的原文'}</span>
        </div>
      )}
      {paragraphs.length
        ? paragraphs.map((paragraph) => <DocumentParagraph key={paragraph.start} paragraph={paragraph} location={location} />)
        : <div className="empty">这份资料没有可显示的正文{material.tables.length ? `，包含 ${material.tables.length} 张数据表` : ''}</div>}
      {material.tables.map((table) => (
        <div className="card" key={table.name} style={{ marginTop: 'var(--s-4)' }}>
          <div className="card-h">{table.name}<span className="spacer" />{table.rowCount} 行</div>
          <div className="card-b t-sm">{table.fields.map((field) => field.name).join(' · ')}</div>
        </div>
      ))}
    </div>
    <aside className="card wb-doc-refs">
      <div className="card-h">{location ? '这处依据被谁引用' : '这份资料被谁引用'}<span className="spacer" />{referrers.length} 条</div>
      <div className="wb-backref">
        {referrers.map((item) => (
          <a key={item.path} onClick={() => actions.selectPath(item.path)} title={item.path}>
            <RefIcon path={item.path} />
            <div>
              <b>{item.path.split('/').pop()}</b>
              <span>{item.why || (item.path.includes('/') ? item.path.split('/').slice(0, -1).join('/') : `第 ${item.line} 行`)}</span>
            </div>
          </a>
        ))}
        {!referrers.length && <div className="empty t-sm">当前知识包还没有引用这份资料</div>}
      </div>
      <div className="card-b" style={{ borderTop: '1px solid var(--c-line-soft)' }}>
        <div className="wb-doc-directives">
          <label><span>用途</span><select className="input" value={material.purpose || 'auto'} aria-label={`${material.filename}的资料用途`}
            onChange={(event) => void actions.setMaterialDirectives(material.id, { purpose: event.target.value })}>
            {['auto', 'goal', 'schema', 'term', 'evidence'].map((value) => <option key={value} value={value}
              disabled={['schema', 'term'].includes(value) && !['excel', 'csv'].includes(material.kind)}>{materialPurposeLabel(value)}</option>)}
          </select></label>
          {material.purpose === 'term' && <button className="btn btn-sm" type="button" onClick={() => void downloadTemplate()}>
            <Icon name="download" size={12} />下载模板
          </button>}
          <label className="wb-material-reading"><input type="checkbox" checked={material.readingMode === 'full'}
            onChange={(event) => void actions.setMaterialDirectives(material.id, { readingMode: event.target.checked ? 'full' : 'on_demand' })} />
            <span>全文精读</span></label>
        </div>
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

function DocumentParagraph({ paragraph, location }) {
  const mark = useRef(null);
  const highlight = evidenceHighlight(paragraph, location);
  useEffect(() => {
    if (highlight) mark.current?.scrollIntoView({ block: 'center', inline: 'nearest' });
  }, [highlight?.start, highlight?.end]);
  const heading = /^#{1,5}\s/u.test(paragraph.text);
  const prefixLength = heading ? paragraph.text.match(/^#{1,5}\s*/u)?.[0].length || 0 : 0;
  const text = heading ? paragraph.text.slice(prefixLength) : paragraph.text;
  const adjusted = highlight ? {
    start: Math.max(0, highlight.start - prefixLength),
    end: Math.max(0, highlight.end - prefixLength),
  } : null;
  const children = adjusted && adjusted.end > adjusted.start
    ? <>{text.slice(0, adjusted.start)}<mark ref={mark} className="wb-evidence-mark">{text.slice(adjusted.start, adjusted.end)}</mark>{text.slice(adjusted.end)}</>
    : text;
  if (heading) return <h5 className={highlight ? 'wb-evidence-target' : undefined}>{children}</h5>;
  return <p className={highlight ? 'wb-evidence-target' : undefined}>{children}</p>;
}
