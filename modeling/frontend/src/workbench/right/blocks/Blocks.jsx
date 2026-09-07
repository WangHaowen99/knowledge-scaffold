import { store, actions, materialPurposeLabel } from '../../lib/appStore.js';
import { useStore } from '../../lib/store.js';
import * as F from '../../lib/mock/fixtures.js';
import Icon from '../../ui/Icon.jsx';
import { useRef, useState } from 'react';
import MarkdownContent from '../../../MarkdownContent.jsx';
import { userFacingText } from '../../lib/userFacingText.js';
import { downloadTermTemplate } from '../../lib/api.js';

export function Block({ block }) {
  switch (block.type) {
    case 'text':      return <div className="wb-text"><MarkdownContent>{userFacingText(block.text)}</MarkdownContent></div>;
    case 'reasoning': return <ReasoningBlock block={block} />;
    case 'plan':      return <PlanBlock b={block} />;
    case 'ask':       return block.interaction && block.interaction.status !== 'pending' ? <AnsweredInteraction interaction={block.interaction} /> : <AskBlock b={block} />;
    case 'material':  return <MaterialRequest />;
    case 'confirm':   return block.interaction && block.interaction.status !== 'pending' ? <AnsweredInteraction interaction={block.interaction} /> : block.interaction ? <LiveConfirm interaction={block.interaction} /> : <ConfirmBlock />;
    case 'release':   return block.interaction && block.interaction.status !== 'pending' ? <AnsweredInteraction interaction={block.interaction} /> : <LiveRelease interaction={block.interaction} />;
    case 'policy':    return <PolicyBlock />;
    case 'preview':   return <PreviewBlock b={block} />;
    case 'error':     return <ErrorBlock block={block} />;
    case 'recovery':  return <RecoveryBlock block={block} />;
    case 'upload':    return <UploadBlock block={block} />;
    case 'uploadResult': return <UploadResult block={block} />;
    default:          return null;
  }
}

function RecoveryBlock({ block }) {
  const [busy, setBusy] = useState('');
  const run = async (action) => {
    if (!action?.id || busy) return;
    setBusy(action.id);
    try {
      if (block.issueId) await actions.resumeIssue(block.issueId, action.id);
      else await actions.send(action.message || '请从已保存的当前进展继续，只处理尚未完成的部分。');
    } finally {
      setBusy('');
    }
  };
  return (
    <div className="card card-wait">
      <div className="card-h">请选择后续处理</div>
      <div className="card-b">
        <div className="wb-text" style={{ marginBottom: 'var(--s-3)' }}>
          <MarkdownContent>{userFacingText(block.text)}</MarkdownContent>
        </div>
        {(block.actions || []).map((action) => (
          <button key={action.id || action.label} type="button" className="wb-opt"
            disabled={Boolean(busy)} onClick={() => void run(action)}>
            <b>{busy === action.id ? '正在处理…' : userFacingText(action.label || '继续')}</b>
            {action.description && <span>{userFacingText(action.description)}</span>}
          </button>
        ))}
      </div>
    </div>
  );
}

function ErrorBlock({ block }) {
  const [busy, setBusy] = useState('');
  const run = async (action) => {
    if (!action?.id || busy) return;
    setBusy(action.id);
    try {
      if (block.issueId) await actions.resumeIssue(block.issueId, action.id);
      else await actions.send(action.message || '请从已保存的当前进展继续，先核对计划和写入回执，不要重复已经完成的修改。');
    } finally {
      setBusy('');
    }
  };
  const repairing = block.status === 'repairing';
  return (
    <div className={`card ${repairing ? 'card-wait' : 'card-stale'}`}>
      <div className="card-h">{repairing ? '系统正在自动修正' : '当前问题需要处理'}</div>
      <div className="card-b">
        <div>{userFacingText(block.text)}</div>
        {Array.isArray(block.actions) && block.actions.length > 0 && (
          <div className="btn-row wb-error-actions">
            {block.actions.map((action) => (
              <button key={action.id || action.label} type="button" className="btn btn-sm"
                disabled={Boolean(busy)} onClick={() => void run(action)}>
                {busy === action.id ? '正在恢复…' : userFacingText(action.label || '重试')}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ReasoningBlock({ block }) {
  const [expanded, setExpanded] = useState(Boolean(block.streaming));
  return (
    <details className="wb-reasoning" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>
        <span>{block.streaming ? '正在思考' : '思考过程'}</span>
        {block.truncated && <small>已收起较长内容</small>}
      </summary>
      <div className="wb-reasoning-body wb-text">
        <MarkdownContent>{userFacingText(block.text)}</MarkdownContent>
      </div>
    </details>
  );
}

function AnsweredInteraction({ interaction }) {
  const cancelled = ['cancelled', 'superseded'].includes(interaction?.status);
  return <div className="card"><div className="card-h">{cancelled ? '已关闭' : '已处理'}</div><div className="card-b t-sm muted"><MarkdownContent>{userFacingText(interaction.payload?.question || (cancelled ? '这项决定已随运行结束关闭' : '这项确认已经提交'))}</MarkdownContent></div></div>;
}

/* ---------- 计划：渲染 .modeling/plan.md，因此天然有版本和回退 ---------- */
function PlanBlock({ b }) {
  const [busy, setBusy] = useState('');
  const [discardPreview, setDiscardPreview] = useState(null);
  const [discardReason, setDiscardReason] = useState('');
  const recovery = useStore(store, (s) => s.recovery);
  const plan = b.plan || {};
  const items = b.items || [];
  const staleBlocked = Boolean(recovery && ['stale_run', 'plan_unreadable'].includes(recovery.status));
  const decide = async (approved) => {
    if (!plan.id || busy || staleBlocked) return;
    setBusy(approved ? 'approve' : 'reject');
    try { await actions.respondChangePlan(plan.id, approved); } finally { setBusy(''); }
  };
  const safeTerminate = async () => {
    if (!plan.id || busy) return;
    setBusy('safe-terminate');
    try { await actions.safeTerminateChangePlan(plan.id); } finally { setBusy(''); }
  };
  const previewDiscard = async () => {
    if (!plan.id || busy) return;
    setBusy('discard-preview');
    try {
      const preview = await actions.previewChangePlanDiscard(plan.id);
      if (preview) {
        setDiscardPreview(preview);
        setDiscardReason('');
      }
    } finally {
      setBusy('');
    }
  };
  const confirmDiscard = async () => {
    if (!plan.id || !discardPreview || !discardReason.trim() || busy) return;
    setBusy('discard');
    try {
      const result = await actions.discardChangePlan(plan.id, discardPreview, discardReason.trim());
      if (result) setDiscardPreview(null);
    } finally {
      setBusy('');
    }
  };
  return (
    <div className="card" data-change-plan-id={plan.id || undefined}
      data-change-plan-version={plan.version ?? undefined}>
      <div className="card-h">当前计划<span className="spacer" />
        {plan.status === 'completed' ? '已完成'
          : plan.status === 'cancelled' ? '已结束'
          : plan.approval_status === 'pending' ? '等待批准'
          : plan.approval_status === 'rejected' ? '已拒绝' : plan.id ? '已批准' : ''}
      </div>
      <div className="card-b">
        <ul className="wb-plan">
          {items.map((it) => (
            <li key={it.t} className={it.st || ''}>
              <span className="bx">{it.st === 'done' ? '[x]' : it.st === 'now' ? '[>]' : '[ ]'}</span>
              <span className="tx">{userFacingText(it.t)}</span>
            </li>
          ))}
        </ul>
        {plan.approval_status === 'pending' && <div className="wb-plan-approval">
          {staleBlocked && (
            <div className="wb-plan-stale-block" role="alert">
              <span>当前运行处于失效占用状态，先安全终止失效计划后才能继续或重新规划。</span>
              <div className="btn-row">
                <button type="button" className="btn btn-p btn-sm" disabled={Boolean(busy)}
                  onClick={() => void safeTerminate()}>{busy === 'safe-terminate' ? '安全终止中…' : '安全终止失效计划'}</button>
              </div>
            </div>
          )}
          {!staleBlocked && <>
            <span>批准前不会生成对象、规则、操作、指标或脚本候选。</span>
            <div className="btn-row">
              <button type="button" className="btn btn-p btn-sm" disabled={Boolean(busy)}
                onClick={() => void decide(true)}>{busy === 'approve' ? '批准中…' : '批准并开始'}</button>
              <button type="button" className="btn btn-sm" disabled={Boolean(busy)}
                onClick={() => void decide(false)}>{busy === 'reject' ? '拒绝中…' : '拒绝计划'}</button>
            </div>
          </>}
        </div>}
        {plan.status === 'active' && plan.mode === 'generation'
          && plan.approval_status === 'approved' && <div className="wb-plan-approval">
          {!discardPreview ? <div className="btn-row">
            <button type="button" className="btn btn-danger btn-sm" disabled={Boolean(busy)}
              onClick={() => void previewDiscard()}>
              {busy === 'discard-preview' ? '正在核对影响…' : '停止并放弃整份计划'}
            </button>
          </div> : <>
            <span>{discardPreview.display?.text || `将删除 ${discardPreview.discard_count || 0} 项候选修改。`}</span>
            {Object.keys(discardPreview.path_counts || {}).length > 0 && <span>
              {Object.entries(discardPreview.path_counts).map(([key, count]) => `${key} ${count} 项`).join('；')}
            </span>}
            <textarea className="input" rows={2} maxLength={1000} value={discardReason}
              placeholder="填写放弃整份计划的原因"
              onChange={(event) => setDiscardReason(event.target.value)} />
            <div className="btn-row">
              <button type="button" className="btn btn-danger btn-sm"
                disabled={Boolean(busy) || !discardReason.trim()}
                onClick={() => void confirmDiscard()}>
                {busy === 'discard' ? '正在停止并放弃…' : '确认放弃这些候选'}
              </button>
              <button type="button" className="btn btn-sm" disabled={Boolean(busy)}
                onClick={() => { setDiscardPreview(null); setDiscardReason(''); }}>保留当前计划</button>
            </div>
          </>}
        </div>}
      </div>
    </div>
  );
}

/* ---------- 问题卡：阻断性问题也不锁死输入框 ---------- */
function AskBlock({ b }) {
  const interaction = b.interaction;
  if (interaction) return <LiveAsk interaction={interaction} />;
  return (
    <div className="card card-wait">
      <div className="card-h">需要你决定 · 业务口径<span className="spacer" />{b.idx}</div>
      <div className="card-b">
        <div style={{ marginBottom: 'var(--s-3)' }}>{b.q}</div>
        {b.options.map((o) => (
          <button key={o.t} className="wb-opt"><b>{o.t}</b><span>{o.s}</span></button>
        ))}
        <div style={{ display: 'flex', gap: 6, marginTop: 'var(--s-2)' }}>
          <input className="input" placeholder="或者直接说明你们的口径…" />
          <button className="btn btn-sm">提交</button>
        </div>
        <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
          {b.chips.map((c) => <button key={c} className="btn btn-sm">{c}</button>)}
        </div>
      </div>
    </div>
  );
}

function LiveAsk({ interaction }) {
  const [manual, setManual] = useState('');
  const [followUpOption, setFollowUpOption] = useState(null);
  const [selectedOption, setSelectedOption] = useState(null);
  const [sending, setSending] = useState(false);
  const manualInput = useRef(null);
  const payload = interaction.payload || {};
  const options = payload.options || [];
  const material = interaction.kind === 'material_request';
  const submit = async (answer) => {
    if (sending) return;
    setSending(true);
    try { await actions.answer(interaction.id, answer); } finally { setSending(false); }
  };
  return (
    <div className={`card ${material ? 'card-accent' : 'card-wait'}`} data-pending-interaction={interaction.id}>
      <div className="card-h">{material
        ? <><Icon name="upload" size={12} />需要补充资料</>
        : options.length ? '需要你决定' : '需要你补充'}</div>
      <div className="card-b">
        <div className="wb-text" style={{ marginBottom: 'var(--s-3)' }}>
          <MarkdownContent>{userFacingText(payload.question)}</MarkdownContent>
        </div>
        {options.map((option) => (
          <button key={option.value || option.label}
            type="button"
            className={`wb-opt ${followUpOption === option || selectedOption === option ? 'on' : ''}`}
            disabled={sending} aria-pressed={followUpOption === option || selectedOption === option}
            aria-expanded={option.requiresText ? followUpOption === option : undefined}
            onClick={() => {
              if (option.requiresText) {
                setFollowUpOption(option);
                setSelectedOption(null);
                setManual('');
                return;
              }
              setFollowUpOption(null);
              setSelectedOption(option);
            }}>
            <b>{userFacingText(option.label)}</b><span>{userFacingText(option.description)}</span>
          </button>
        ))}
        {material && (
          <button className="wb-drop sm" style={{ width: '100%', marginTop: 'var(--s-2)' }}
            onClick={() => document.getElementById('wb-inline-upload')?.click()}>
            <span className="t2">选择文件，或拖到输入框</span>
          </button>
        )}
        {followUpOption ? (
          <form className="wb-ask-followup" onSubmit={(event) => {
            event.preventDefault();
            const value = followUpOption.value || followUpOption.label;
            void submit({
              value,
              selected: [value],
              text: manual.trim(),
              ...(material && value === 'oral' ? { action: 'oral' } : {}),
            });
          }}>
            <label htmlFor={`ask-followup-${interaction.id}`}>
              {userFacingText(followUpOption.textPrompt || '请补充具体说明')}
            </label>
            <textarea id={`ask-followup-${interaction.id}`} ref={manualInput} autoFocus rows={3}
              value={manual} onChange={(event) => setManual(event.target.value)} />
            <div className="wb-ask-followup-actions">
              <button type="button" className="btn btn-sm" disabled={sending}
                onClick={() => { setFollowUpOption(null); setManual(''); }}>返回</button>
              <button type="submit" className="btn btn-p btn-sm" disabled={sending || !manual.trim()}>提交</button>
            </div>
          </form>
        ) : (
          <form className="wb-ask-manual wb-ask-decision" onSubmit={(event) => {
            event.preventDefault();
            if (selectedOption) {
              const value = selectedOption.value || selectedOption.label;
              void submit({ value, selected: [value] });
              return;
            }
            void submit({ value: manual.trim(), action: material ? 'oral' : 'manual' });
          }}>
            <input ref={manualInput} className="input" value={manual} onChange={(event) => setManual(event.target.value)}
              disabled={Boolean(selectedOption)}
              placeholder={selectedOption
                ? `已选择：${userFacingText(selectedOption.label)}`
                : material ? '也可以直接口述内容…' : '或者直接说明你们的口径…'} />
            {selectedOption && <button type="button" className="btn btn-sm" disabled={sending}
              onClick={() => setSelectedOption(null)}>重选</button>}
            <button type="submit" className="btn btn-p btn-sm"
              disabled={sending || (!selectedOption && !manual.trim())}>{sending ? '提交中…' : '确认提交'}</button>
          </form>
        )}
      </div>
    </div>
  );
}

function LiveConfirm({ interaction }) {
  const reviewed = useStore(store, (state) => state.reviewedChangeGroups);
  const payload = interaction.payload || {};
  const groups = payload.changeGroups || [];
  const atomicSelection = payload.atomicSelection === true;
  const selectable = groups.filter((group) => !group.autoApproved);
  const [selected, setSelected] = useState(() => atomicSelection ? selectable.map((group) => group.id) : []);
  const [reviewMode, setReviewMode] = useState('full');
  const [returning, setReturning] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [sending, setSending] = useState(false);
  const feedbackInput = useRef(null);
  const sampled = selectable.slice(0, 3).map((group) => group.id);
  const selectedForSubmit = atomicSelection ? selectable.map((group) => group.id) : selected;
  const reviewRequired = reviewMode === 'sampled' ? sampled : selectedForSubmit;
  const allReviewed = reviewRequired.length > 0 && reviewRequired.every((id) => reviewed.includes(id));
  const autoApprovedCount = groups.length - selectable.length;
  const heldCount = selectable.length - selectedForSubmit.length;
  const respond = async (answer) => {
    if (sending) return;
    setSending(true);
    try { await actions.answer(interaction.id, answer); } finally { setSending(false); }
  };
  return (
    <div className="card card-accent" data-pending-interaction={interaction.id}>
      <div className="card-h">确认待修改内容<span className="spacer" />已查看 {reviewed.filter((id) => groups.some((group) => group.id === id)).length}/{groups.length} · 已选 {selectedForSubmit.length}</div>
      <div className="card-b">
        <div className="wb-confirm-semantics">
          接受后才会写入正式版本；退回调整会保留 Studio 工作草稿并解除编辑锁，方便继续修改。
        </div>
        {payload.lastSubmissionError && <div className="wb-confirm-submit-error" role="alert">
          <b>刚才没有接受成功</b>
          <span>{userFacingText(payload.lastSubmissionError)}</span>
          <small>候选内容仍然保留；原因处理完成后可再次接受，也可以退回调整。</small>
        </div>}
        {groups.map((group) => {
          const impact = (group.impact || []).filter(Boolean);
          const hasReviewed = reviewed.includes(group.id);
          const path = impact[0] || '';
          return <div className={`wb-grp ${group.autoApproved ? 'dim' : ''}${hasReviewed ? ' reviewed' : ''}`} key={group.id}>
            <input type="checkbox" disabled={group.autoApproved || atomicSelection || sending}
              checked={group.autoApproved || atomicSelection || selected.includes(group.id)}
              aria-label={`选择${userFacingText(group.title)}`}
              onChange={() => setSelected((items) => items.includes(group.id)
                ? items.filter((id) => id !== group.id) : [...items, group.id])} />
            <span><b>{userFacingText(group.title)}</b><span className="gm">{group.tier === 'caliber' ? '业务口径' : group.tier === 'structural' ? '结构变更' : group.tier === 'script' ? '脚本' : '自动同步'}{group.warning ? ` · ${userFacingText(group.warning)}` : ''}</span>
              {impact.length > 0 && <span className="gi" title={impact.join('\n')}>
                涉及 {impact.slice(0, 2).join('、')}{impact.length > 2 ? ` 等 ${impact.length} 处` : ''}
              </span>}
            </span>
            <button type="button" className="btn btn-sm wb-review-group"
              onClick={() => actions.reviewChangeGroup(group.id, path)}>
              {hasReviewed ? '已查看' : '查看变更'}
            </button>
          </div>;
        })}
        {returning ? <>
          <textarea ref={feedbackInput} className="input" rows={3} aria-label="调整要求"
            style={{ width: '100%', marginTop: 'var(--s-4)' }} value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder="请说明需要修改的内容；也可以留空，让助手重新检查" />
          <div className="btn-row" style={{ marginTop: 'var(--s-2)' }}>
            <button className="btn btn-danger" disabled={sending}
              onClick={() => void respond({ approved: false, feedback: feedback.trim() || '请重新检查并调整这批候选' })}>
              提交调整要求
            </button>
            <button className="btn" disabled={sending} onClick={() => setReturning(false)}>取消</button>
          </div>
        </> : <>
          <div className="wb-confirm-summary" role="status">
            将接受 {selectedForSubmit.length} 组，暂不接受 {heldCount} 组，自动同步 {autoApprovedCount} 组。
            {!allReviewed && reviewRequired.length > 0 && <b> 请先查看所有将接受的变更。</b>}
          </div>
          <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
          <button className="btn btn-p" disabled={sending || !allReviewed} aria-busy={sending}
            onClick={() => void respond({
              approved: true,
              review_mode: reviewMode,
              selected_groups: selectedForSubmit,
              sampled_groups: reviewMode === 'sampled' ? sampled : [],
            })}>
            {sending ? '正在接受…' : reviewMode === 'sampled'
              ? `接受已查看的前 ${sampled.length} 组`
              : `接受已选（${selectedForSubmit.length}）`}
          </button>
          {!atomicSelection && selectable.length > 3 && <button className="btn" disabled={sending}
            onClick={() => {
              setReviewMode('sampled');
              setSelected(sampled);
            }}>查看前三组</button>}
          {reviewMode === 'sampled' && <button className="btn" disabled={sending}
            onClick={() => { setReviewMode('full'); setSelected([]); }}>改为逐组审阅</button>}
          <button className="btn btn-danger" disabled={sending}
            onClick={() => {
              setReturning(true);
              requestAnimationFrame(() => feedbackInput.current?.focus());
            }}>退回调整</button>
          </div>
        </>}
      </div>
    </div>
  );
}

function LiveRelease({ interaction }) {
  const [sending, setSending] = useState(false);
  const payload = interaction.payload || {};
  const questions = payload.questions || [];
  const respond = async (answer) => {
    if (sending) return;
    setSending(true);
    try { await actions.answer(interaction.id, answer); } finally { setSending(false); }
  };
  return (
    <div className="card card-accent wb-release-confirm" data-pending-interaction={interaction.id}>
      <div className="card-h">正式发布确认<span className="spacer" /><code className="k">{payload.version}</code></div>
      <div className="card-b">
        <div className="wb-release-summary">
          <b>{payload.testcaseCount} 条真实用例已全部通过</b>
          <span>请确认这些用例覆盖本次发布范围；发布后将生成稳定版本 {payload.version}。</span>
        </div>
        {questions.length > 0 && <details className="wb-release-cases">
          <summary>查看本次评测范围（{questions.length}）</summary>
          <ol>{questions.map((question) => <li key={question}>{userFacingText(question)}</li>)}</ol>
        </details>}
        <div className="btn-row" style={{ marginTop: 'var(--s-4)' }}>
          <button className="btn btn-p" disabled={sending} onClick={() => void respond({
            approved: true,
            coverage_confirmed: true,
            version: payload.version,
            displayText: `已确认用例覆盖，发布 ${payload.version}`,
          })}>确认覆盖并发布</button>
          <button className="btn" disabled={sending} onClick={() => void respond({
            approved: false,
            coverage_confirmed: false,
            version: payload.version,
            feedback: '继续补充真实用例，暂不发布',
            displayText: '继续补充真实用例，暂不发布',
          })}>继续补充用例</button>
        </div>
      </div>
    </div>
  );
}

function UploadBlock({ block }) {
  const downloadTemplate = async () => {
    const response = await downloadTermTemplate(store.get().projectId);
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'term-table-template.xlsx';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };
  const uploading = block.files.filter((file) => file.status === 'uploading').length;
  const parsed = block.files.filter((file) => file.status === 'parsed').length;
  const failed = block.files.filter((file) => !['uploading', 'parsing', 'parsed'].includes(file.status)).length;
  const completed = parsed + failed;
  const title = uploading
    ? `正在上传 ${block.files.length - uploading}/${block.files.length}`
    : completed < block.files.length
    ? `后台解析 ${completed}/${block.files.length}`
    : failed
      ? `解析结果 ${parsed} 成功 / ${failed} 失败`
      : `解析成功 ${parsed}/${block.files.length}`;
  return <div className={`card${failed ? ' card-stale' : ''}`}><div className="card-h"><Icon name="upload" size={12} />{title}</div>
    <div className="card-b">{block.files.map((file) => {
      const readable = file.status === 'parsed';
      const unavailable = !['uploading', 'parsing', 'parsed'].includes(file.status);
      const status = file.status === 'uploading' ? '正在上传'
        : readable ? '可读取'
          : unavailable ? '无法解析' : '已上传，后台解析中';
      const purpose = file.purpose || 'auto';
      const readingMode = file.readingMode || 'on_demand';
      const canConfigure = Boolean(file.documentId && !unavailable);
      const table = ['excel', 'csv'].includes(file.kind) || /\.(?:xlsx?|csv)$/i.test(file.name);
      return <div className="wb-upload-file" key={file.id}>
        <div className="wb-file"><span className={`dot dot-${readable ? 'ok' : unavailable ? 'stale' : 'new'}`} /><span className="fn">{file.name}</span><span className="fs">{file.error || status}</span></div>
        {canConfigure && <div className="wb-material-directives">
          <label><span>用途</span><select className="input" aria-label={`${file.name}的资料用途`} value={purpose}
            onChange={(event) => void actions.setMaterialDirectives(file.documentId, { purpose: event.target.value })}>
            {['auto', 'goal', 'schema', 'term', 'evidence'].map((value) => <option key={value} value={value} disabled={['schema', 'term'].includes(value) && !table}>{materialPurposeLabel(value)}</option>)}
          </select></label>
          {purpose === 'term' && <button className="btn btn-sm" type="button" onClick={() => void downloadTemplate()}>
            <Icon name="download" size={12} />下载模板
          </button>}
          <label className="wb-material-reading"><input type="checkbox" checked={readingMode === 'full'}
            onChange={(event) => void actions.setMaterialDirectives(file.documentId, { readingMode: event.target.checked ? 'full' : 'on_demand' })} />
            <span>全文精读</span></label>
        </div>}
      </div>;
    })}</div></div>;
}

function UploadResult({ block }) {
  const title = block.failed
    ? `解析结果 ${block.parsed} 成功 / ${block.failed} 失败`
    : `解析成功 ${block.parsed}/${block.parsed}`;
  return <div className={`card ${block.failed ? 'card-stale' : ''}`}><div className="card-h">{title}</div>
    <div className="card-b">{block.details.map((file) => <div className="wb-file" key={file.name}><span className={`dot dot-${file.status === 'parsed' ? 'ok' : 'stale'}`} /><span className="fn">{file.name}</span><span className="fs">{file.error || (file.status === 'parsed' ? '可读取' : '无法解析')}</span></div>)}</div></div>;
}

/* ---------- 资料请求：「我口述」是必须有的出口 ---------- */
function MaterialRequest() {
  return (
    <div className="card card-accent">
      <div className="card-h"><Icon name="upload" size={12} />需要补充资料</div>
      <div className="card-b">
        <div style={{ marginBottom: 'var(--s-3)' }}>
          现有资料里没有说明<b>安全库存本身是怎么定的</b>——是拍的、按历史消耗算的，还是按供货周期算的？
          这会影响 D2 之后所有分支。
        </div>
        <div className="wb-drop sm"><div className="t2">把文件拖到这里，或点击选择</div></div>
        <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
          <button className="btn btn-p">我口述</button>
          <button className="btn">没有这方面资料，跳过</button>
          <button className="btn">让我先找找</button>
        </div>
        <div className="t-sm muted" style={{ marginTop: 'var(--s-3)' }}>
          口述内容会作为业务依据保留，并和文档依据一同参与后续建模。
        </div>
      </div>
    </div>
  );
}

/* ---------- 方案预览：动作明确，但不代替确认 ---------- */
function PreviewBlock({ b }) {
  return (
    <div className="card">
      <div className="card-h">{b.title}</div>
      <div className="card-b">
        <div className="t-sm muted">{b.text}</div>
        <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
          {b.actions.map((a) => (
            <button key={a.t} disabled={a.disabled}
              className={`btn btn-sm${a.primary ? ' btn-p' : ''}${a.danger ? ' btn-danger' : ''}`}>{a.t}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ---------- 确认卡：分诊 + 批量 + 策略 ---------- */
const selApproved = (s) => s.approved;
const selOpenTiers = (s) => s.openTiers;
const selReview = (s) => s.reviewMode;

function ConfirmBlock() {
  const approved = useStore(store, selApproved);
  const openTiers = useStore(store, selOpenTiers);
  const review = useStore(store, selReview);
  const byTier = (t) => F.changeGroups.filter((g) => g.tier === t);
  const total = F.changeGroups.length;
  const okCount = Object.values(approved).filter(Boolean).length;

  return (
    <>
      <div className="card card-accent">
        <div className="card-h">确认待修改内容<span className="spacer" />{total} 组 · 23 文件</div>
        <div className="card-b">
          {Object.entries(F.TIERS).map(([tier, meta]) => {
            const groups = byTier(tier);
            if (!groups.length) return null;
            const open = openTiers[tier];
            const allOn = groups.every((g) => approved[g.id]);
            return (
              <div key={tier} className={`wb-tier ${tier}${open ? ' open' : ''}`}>
                <button className="th" onClick={() => actions.toggleTier(tier)} aria-expanded={open}>
                  <input type="checkbox" checked={allOn} onClick={(e) => e.stopPropagation()}
                    onChange={(e) => actions.setTierApproved(tier, e.target.checked)} />
                  <span className="nm">{meta.label}</span>
                  <span className="ct">
                    {groups.length} 项{tier === 'structural' ? ' · 对象字段、业务操作步骤' : ''}
                    {tier === 'index' ? ' · 已设为自动' : ''}
                  </span>
                  <span className="spacer" />
                  <Icon name={open ? 'chevronUp' : 'chevron'} size={12} />
                </button>
                {open && (
                  <div className="tb">
                    {groups.map((g) => (
                      <div className="wb-grp" key={g.id}>
                        <input type="checkbox" checked={!!approved[g.id]} onChange={() => actions.toggleGroup(g.id)} />
                        <div style={{ minWidth: 0 }}>
                          <div className="gt">{g.title}</div>
                          {g.meta && <div className="gm">{g.meta}</div>}
                          {g.evidence && <div className="gm">证据 <code className="k">{g.evidence}</code>{g.impact ? ` · 影响 ${g.impact} 处` : ''}</div>}
                          {g.warning && <div className="gw">⚠ {g.warning}</div>}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}

          <div className="btn-row" style={{ marginTop: 'var(--s-4)' }}>
            <button className="btn btn-p" onClick={() => actions.setReviewMode('full')}>全部接受（{total}）</button>
            <button className="btn" onClick={() => actions.setReviewMode('sequential')}>逐条过（{byTier('caliber').length}）</button>
            <button className="btn" onClick={() => actions.setReviewMode('sampled')}>抽检 3 条</button>
            <button className="btn btn-danger">全部退回</button>
          </div>
          <div className="t-sm muted" style={{ marginTop: 'var(--s-2)' }}>
            已勾选 {okCount} / {total}。「抽检」会随机挑 3 条给你看，其余接受，并在审计记录里标明这次是抽检通过。
          </div>
          {review && (
            <div className="pill pill-accent" style={{ marginTop: 'var(--s-3)' }}>
              本次复核方式：{{ full: '全部接受', sequential: '逐条过', sampled: '抽检 3 条' }[review]} · 会写入确认记录
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ---------- 常设策略：显式开关，不是隐藏偏好 ---------- */
const selPolicy = (s) => s.policy;

function PolicyBlock() {
  const policy = useStore(store, selPolicy);
  return (
    <div>
      <div className="wb-policy">
        <div className="ph">常设策略 · 对这个项目一直生效</div>
        {policy.map((p) => {
          const meta = F.TIERS[p.tier] || {};
          return (
            <div className="wb-prow" key={p.tier}>
              <div className="pn">
                {p.label || meta.label}
                <span>{p.hint || meta.hint}</span>
              </div>
              {p.locked ? (
                <div className="seg seg-fixed">
                  <button className="on">必须问</button>
                  <button disabled>不可关闭</button>
                </div>
              ) : (
                <div className="seg">
                  <button className={p.mode === 'ask' ? 'on' : ''} onClick={() => actions.setPolicy(p.tier, 'ask')}>问我</button>
                  <button className={p.mode === 'auto' ? 'on' : ''} onClick={() => actions.setPolicy(p.tier, 'auto')}>不用问我</button>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="t-sm muted" style={{ marginTop: 'var(--s-2)' }}>
        策略随时可改，只影响之后的确认，不回溯已生效的记录。
      </div>
    </div>
  );
}
