// 流程图结构修复面板（develop !613）：标准编辑器打不开、但能逐项修好的图，占画布的位置列出问题，左边预览右边改。
// 改的是 xml 这份原文，由宿主（流程图弹窗）持有；保存也在宿主的「保存」上。
import { useEffect, useMemo, useState } from 'react';
import {
  Button, Inp, Modal, Note, Select, cx,
} from '@onto/ui/index.jsx';
import { FlowchartSvg } from './FlowShapes.jsx';
import {
  applyDrawioRepair, inspectDrawioRepair, keepRepairDrafts, repairIssueKey, repairIssueLabel, repairPreviewGraph, repairSelection,
  validateRepairedDrawio,
} from './drawioRepair.js';

const DUPLICATE_MODES = [{ value: 'rename', label: '为此节点重新编号' }, { value: 'delete', label: '删除此重复节点' }];
const DANGLING_MODES = [
  { value: 'create', label: '补建缺失节点' }, { value: 'existing', label: '连接现有节点' }, { value: 'delete', label: '删除这条连线' },
];

/**
 * @param {object} p
 * @param {string} p.xml 正在修的 drawio 原文
 * @param {(xml: string) => void} p.onChange 修好一项
 * @param {boolean} p.readOnly 写入范围锁住时只读
 */
export default function DrawioRepairPanel({ xml, onChange, readOnly = false }) {
  const [drafts, setDrafts] = useState({});
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [confirm, setConfirm] = useState(null);
  const issues = useMemo(() => inspectDrawioRepair(xml), [xml]);
  const graph = useMemo(() => repairPreviewGraph(xml), [xml]);
  const focused = issues.find((issue) => repairIssueKey(issue) === selected);

  // 确认框开着时 Esc 只关确认框：弹窗看 defaultPrevented 决定要不要跟着关
  useEffect(() => {
    if (!confirm) return undefined;
    const h = (e) => { if (e.key === 'Escape') e.preventDefault(); };
    document.addEventListener('keydown', h, true);
    return () => document.removeEventListener('keydown', h, true);
  }, [confirm]);

  const draftOf = (issue) => drafts[repairIssueKey(issue)]
    || (issue.type === 'duplicate' ? { mode: 'rename', id: issue.suggestion } : {});
  const setDraft = (issue, draft) => setDrafts((d) => ({ ...d, [repairIssueKey(issue)]: draft }));

  const apply = (issue, replacement) => {
    try {
      const repaired = applyDrawioRepair(xml, issue, replacement);
      const remaining = inspectDrawioRepair(repaired);
      if (!remaining.length) validateRepairedDrawio(repaired);
      setDrafts(keepRepairDrafts(xml, issues, remaining, drafts, issue, replacement));
      setSelected('');
      setError('');
      onChange(repaired);
    } catch (e) {
      setError(e.message);
    }
  };
  const repair = (issue) => {
    const replacement = draftOf(issue);
    if (replacement.mode === 'delete') setConfirm({ issue, replacement });
    else apply(issue, replacement);
  };

  const asking = confirm?.issue;
  return (
    <div className="fc-repair">
      <div className="fc-repair-cv" role="img" aria-label="修复预览">
        <FlowchartSvg model={graph} selection={repairSelection(graph, focused)} />
        <span className="fc-hint">重叠节点在预览中已错开；点击右侧问题可定位。</span>
      </div>
      <div className="fc-repair-list">
        {issues.length
          ? <Note tone="amber">请修复以下结构问题</Note>
          : <Note tone="teal" icon="check">结构问题已全部修复，保存后回到标准编辑器</Note>}
        {issues.map((issue, i) => {
          const key = repairIssueKey(issue);
          const draft = draftOf(issue);
          const duplicate = issue.type === 'duplicate';
          return (
            <div key={key} className={cx('fc-repair-it', key === selected && 'on')}>
              <button type="button" className="fc-repair-t" title={issue.title} onClick={() => setSelected(key)}>
                {i + 1}. {repairIssueLabel(issue)}
              </button>
              <Select
                value={draft.mode} options={duplicate ? DUPLICATE_MODES : DANGLING_MODES} placeholder="选择修复方式" disabled={readOnly}
                onChange={(mode) => setDraft(issue, duplicate ? { ...draft, mode } : { mode })}
              />
              {draft.mode === 'rename' ? (
                <Inp
                  aria-label="新节点 ID" placeholder="新节点 ID" value={draft.id || ''} disabled={readOnly}
                  onChange={(e) => setDraft(issue, { ...draft, id: e.target.value })}
                />
              ) : null}
              {draft.mode === 'create' ? (
                <Inp
                  aria-label="新节点名称" placeholder="输入新节点名称" value={draft.label || ''} disabled={readOnly}
                  onChange={(e) => setDraft(issue, { ...draft, label: e.target.value })}
                />
              ) : null}
              {draft.mode === 'existing' ? (
                <Select
                  value={draft.nodeId} options={issue.options} placeholder="选择现有节点" disabled={readOnly}
                  onChange={(nodeId) => setDraft(issue, { ...draft, nodeId })}
                />
              ) : null}
              <Button size="sm" disabled={readOnly} onClick={() => repair(issue)}>应用修复</Button>
            </div>
          );
        })}
        {error ? <Note tone="red">{error}</Note> : null}
      </div>
      <Modal
        open={!!confirm} onClose={() => setConfirm(null)} icon="trash" danger w={440}
        title={asking?.type === 'duplicate' ? '删除重复节点？' : '删除这条连线？'}
        foot={(
          <>
            <Button onClick={() => setConfirm(null)}>取消</Button>
            <Button kind="danger" onClick={() => { setConfirm(null); apply(confirm.issue, confirm.replacement); }}>确定</Button>
          </>
        )}
      >
        <div className="sm muted">
          {asking?.type === 'duplicate'
            ? `将删除节点「${asking.label}」。它与「${asking.firstLabel}」共用编号，相关连线原本指向哪个节点无法区分。`
            : `将删除连线「${asking?.edgeLabel || asking?.edgeId || '未命名连线'}」及其关联的文字标注。`}
        </div>
      </Modal>
    </div>
  );
}
