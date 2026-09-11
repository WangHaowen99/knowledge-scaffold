import { useEffect, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { diff } from '../lib/mock/fixtures.js';
import { actions, store } from '../lib/appStore.js';
import {
  displayEvidenceReferences,
  evidenceReferenceFromHref,
  remarkEvidenceLinks,
} from '../lib/evidence.js';
import { useStore } from '../lib/store.js';
import { MermaidPreview } from './FlowView.jsx';
import { EvidenceLink } from './EvidenceLinks.jsx';

const TABS = ['Diff', '渲染对照', '它会影响谁'];

export default function DiffView() {
  const [tab, setTab] = useState(0);
  const demo = useStore(store, (state) => state.demo);
  const focus = useStore(store, (state) => state.focus);
  const candidate = useStore(store, (state) => state.candidateDiff);
  const materials = useStore(store, (state) => state.materials);
  const interaction = useStore(store, (state) => state.interaction);
  const groups = interaction?.kind === 'confirmation' ? (interaction.payload?.changeGroups || []) : [];
  const currentIndex = Math.max(0, candidate.findIndex((file) => file.path === focus.target));
  const current = candidate[currentIndex] || candidate[0];
  const currentGroup = groups.find((group) => (group.impact || []).includes(current?.path));
  useEffect(() => {
    if (current?.path && currentGroup?.id) actions.reviewChangeGroup(currentGroup.id, current.path);
  }, [current?.path, currentGroup?.id]);
  if (!demo) {
    return (
      <>
        {!candidate.length && <div className="empty">当前没有可对照的候选修改</div>}
        {current && <div className="wb-diff-batch-nav">
          <div><b>{currentGroup ? `第 ${Math.max(1, groups.indexOf(currentGroup) + 1)} 组 / 共 ${groups.length} 组` : `第 ${currentIndex + 1} 个文件 / 共 ${candidate.length} 个文件`}</b><span>{currentGroup?.title || '整批候选修改'}</span></div>
          <div className="btn-row"><button type="button" className="btn btn-sm" disabled={currentIndex <= 0}
            onClick={() => openCandidate(candidate, currentIndex - 1)}>上一项</button><button type="button" className="btn btn-sm" disabled={currentIndex >= candidate.length - 1}
            onClick={() => openCandidate(candidate, currentIndex + 1)}>下一项</button></div>
        </div>}
        {current && <CandidateFileDiff key={current.path} file={current} materials={materials} />}
      </>
    );
  }
  return (
    <>
      <div className="tabs" style={{ marginBottom: 'var(--s-5)' }} role="tablist">
        {TABS.map((t, i) => (
          <button key={t} role="tab" aria-selected={tab === i}
            className={tab === i ? 'on' : ''} onClick={() => setTab(i)}>{t}</button>
        ))}
      </div>

      {tab === 0 && diff.map((f) => (
        <div className="wb-diff" key={f.path}>
          <div className="fh">
            <span className="path">{f.path}</span>
            <span className="spacer" />
            <span className="t-sm muted">{f.stat}</span>
          </div>
          <pre>{f.lines.map(([k, text], i) => <span className={`l ${k}`} key={i}>{text}{'\n'}</span>)}</pre>
        </div>
      ))}

      {tab === 1 && <div className="empty">渲染前后对照（流程图类变更在此并排显示）</div>}
      {tab === 2 && <Impact />}

      {tab === 0 && (
        <>
          <div className="card">
            <div className="card-h">接受后会影响</div>
            <div className="card-b">
              <div className="t-sm">
                <code className="k">operations/生成采购建议.md</code> · <code className="k">diagrams/采购申请.mmd</code> 的 D2 注释
              </div>
              <div className="t-sm muted" style={{ marginTop: 3 }}>关联用例 <b>0</b> 条需重跑（尚未进入验证）</div>
            </div>
          </div>
          <div className="btn-row" style={{ marginTop: 'var(--s-5)' }}>
            <button className="btn btn-p">接受这条，看下一条</button>
            <button className="btn">退回并说明原因</button>
          </div>
        </>
      )}
    </>
  );
}

function openCandidate(candidate, index) {
  const file = candidate[index];
  if (!file) return;
  store.set({
    selectedPath: file.path,
    focus: { kind: 'diff', target: file.path },
    follow: false,
    pendingFocus: null,
  });
}

function CandidateFileDiff({ file, materials }) {
  const sourceDiff = <DiffSource file={file} materials={materials} className="wb-flow-source" />;
  const graphical = file.path.toLowerCase().endsWith('.mmd') && String(file.after || '').trim();
  const markdown = isMarkdownPath(file.path) && String(file.after || '').trim();
  if (markdown) return <MarkdownCandidatePreview file={file} materials={materials} sourceDiff={sourceDiff} />;
  return (
    <div className={`wb-diff${graphical ? ' wb-diff-mermaid' : ''}`}>
      <div className="fh">
        <span className="path">{file.path}</span><span className="spacer" />
        <span className="t-sm muted">+{file.additions} / -{file.deletions}</span>
      </div>
      {graphical ? (
        <div className="wb-diff-preview">
          <MermaidPreview source={file.after} path={file.path} sourceLabel="源码差异"
            sourceView={sourceDiff} note="当前展示待确认版本；接受后才会写入正式修订" />
        </div>
      ) : sourceDiff}
    </div>
  );
}

function MarkdownCandidatePreview({ file, materials, sourceDiff }) {
  const [mode, setMode] = useState('rendered');
  const content = stripFrontmatter(file.after);
  return (
    <div className="wb-diff wb-diff-markdown">
      <div className="fh">
        <span className="path">{file.path}</span><span className="spacer" />
        <span className="t-sm muted">+{file.additions} / -{file.deletions}</span>
      </div>
      <div className="wb-diff-preview">
        <div className="wb-flow-toolbar">
          <div className="seg" role="tablist" aria-label="Markdown 候选查看方式">
            <button role="tab" aria-selected={mode === 'rendered'} className={mode === 'rendered' ? 'on' : ''}
              onClick={() => setMode('rendered')}>渲染</button>
            <button role="tab" aria-selected={mode === 'source'} className={mode === 'source' ? 'on' : ''}
              onClick={() => setMode('source')}>源码差异</button>
          </div>
        </div>
        {mode === 'rendered'
          ? <article className="wb-markdown"><Markdown
            remarkPlugins={[remarkGfm, remarkEvidenceLinks(materials)]} components={{
              a: ({ node: _node, href, children, ...props }) => {
                const reference = evidenceReferenceFromHref(href);
                return reference
                  ? <EvidenceLink reference={reference} materials={materials}>{children}</EvidenceLink>
                  : <a href={href} {...props}>{children}</a>;
              },
            }}>{content}</Markdown></article>
          : sourceDiff}
      </div>
    </div>
  );
}

function DiffSource({ file, materials, className = '' }) {
  return (
    <pre className={className}>{diffLines(file).map(([kind, line], index) => (
      <span className={`l ${kind}`} key={`${index}-${line}`}>
        {kind === 'add' ? '+ ' : kind === 'del' ? '- ' : '  '}{displayEvidenceReferences(line, materials)}{'\n'}
      </span>
    ))}</pre>
  );
}

export function diffLines(file) {
  if (Array.isArray(file.hunks)) {
    return file.hunks.flatMap((hunk) => (hunk.lines || []).map((line) => [
      line.type === 'remove' ? 'del' : line.type === 'add' ? 'add' : 'ctx',
      line.content || '',
    ]));
  }
  const beforeText = String(file.before || '');
  const afterText = String(file.after || '');
  const before = beforeText ? beforeText.split('\n') : [];
  const after = afterText ? afterText.split('\n') : [];
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < before.length - prefix && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]) suffix += 1;
  return [
    ...before.slice(Math.max(0, prefix - 2), prefix).map((line) => ['ctx', line]),
    ...before.slice(prefix, before.length - suffix).map((line) => ['del', line]),
    ...after.slice(prefix, after.length - suffix).map((line) => ['add', line]),
    ...after.slice(after.length - suffix, Math.min(after.length, after.length - suffix + 2)).map((line) => ['ctx', line]),
  ];
}

function isMarkdownPath(path) {
  return /\.(?:md|markdown)$/iu.test(String(path || ''));
}

function stripFrontmatter(markdown) {
  return String(markdown || '').replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '');
}

function Impact() {
  return (
    <div className="card">
      <div className="card-h">这条规则被谁引用</div>
      <div className="wb-backref">
        <a><b>operations/生成采购建议.md</b><span>步骤 3：判断是否触发补货</span></a>
        <a><b>diagrams/采购申请.mmd</b><span>节点 D2 的注释</span></a>
      </div>
    </div>
  );
}
