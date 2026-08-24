import { store, actions, materialPurposeLabel } from '../../lib/appStore.js';
import { useStore } from '../../lib/store.js';
import * as F from '../../lib/mock/fixtures.js';
import Icon from '../../ui/Icon.jsx';
import { useRef, useState } from 'react';
import MarkdownContent from '../../../MarkdownContent.jsx';
import { userFacingText } from '../../lib/userFacingText.js';

export function Block({ block }) {
  switch (block.type) {
    case 'text':      return <div className="wb-text"><MarkdownContent>{userFacingText(block.text)}</MarkdownContent></div>;
    case 'plan':      return <PlanBlock items={block.items} />;
    case 'ask':       return block.interaction?.status === 'answered' ? <AnsweredInteraction interaction={block.interaction} /> : <AskBlock b={block} />;
    case 'material':  return <MaterialRequest />;
    case 'confirm':   return block.interaction?.status === 'answered' ? <AnsweredInteraction interaction={block.interaction} /> : block.interaction ? <LiveConfirm interaction={block.interaction} /> : <ConfirmBlock />;
    case 'release':   return block.interaction?.status === 'answered' ? <AnsweredInteraction interaction={block.interaction} /> : <LiveRelease interaction={block.interaction} />;
    case 'policy':    return <PolicyBlock />;
    case 'preview':   return <PreviewBlock b={block} />;
    case 'error':     return <div className="card card-stale"><div className="card-b">{userFacingText(block.text)}</div></div>;
    case 'upload':    return <UploadBlock block={block} />;
    case 'uploadResult': return <UploadResult block={block} />;
    default:          return null;
  }
}

function AnsweredInteraction({ interaction }) {
  return <div className="card"><div className="card-h">已处理</div><div className="card-b t-sm muted"><MarkdownContent>{userFacingText(interaction.payload?.question || '这项确认已经提交')}</MarkdownContent></div></div>;
}

/* ---------- 计划：渲染 .modeling/plan.md，因此天然有版本和回退 ---------- */
function PlanBlock({ items }) {
  return (
    <div className="card">
      <div className="card-h">当前计划</div>
      <div className="card-b">
        <ul className="wb-plan">
          {items.map((it) => (
            <li key={it.t} className={it.st || ''}>
              <span className="bx">{it.st === 'done' ? '[x]' : it.st === 'now' ? '[>]' : '[ ]'}</span>
              <span className="tx">{userFacingText(it.t)}</span>
            </li>
          ))}
        </ul>
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
    <div className={`card ${material ? 'card-accent' : 'card-wait'}`}>
      <div className="card-h">{material ? <><Icon name="upload" size={12} />需要补充资料</> : '需要你决定'}</div>
      <div className="card-b">
        <div className="wb-text" style={{ marginBottom: 'var(--s-3)' }}>
          <MarkdownContent>{userFacingText(payload.question)}</MarkdownContent>
        </div>
        {options.map((option) => (
          <button key={option.value || option.label} className="wb-opt" disabled={sending}
            onClick={() => {
              const value = option.value || option.label;
              if (material && value === 'oral') {
                manualInput.current?.focus();
                return;
              }
              void submit({ value, selected: [value] });
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
        <div style={{ display: 'flex', gap: 6, marginTop: 'var(--s-3)' }}>
          <input ref={manualInput} className="input" value={manual} onChange={(event) => setManual(event.target.value)}
            placeholder={material ? '也可以直接口述内容…' : '或者直接说明你们的口径…'} />
          <button className="btn btn-p btn-sm" disabled={sending || !manual.trim()}
            onClick={() => void submit({ value: manual.trim(), action: material ? 'oral' : 'manual' })}>提交</button>
        </div>
      </div>
    </div>
  );
}

function LiveConfirm({ interaction }) {
  const policy = useStore(store, (state) => state.confirmPolicy);
  const payload = interaction.payload || {};
  const groups = payload.changeGroups || [];
  const selectable = groups.filter((group) => !group.autoApproved);
  const [selected, setSelected] = useState(() => selectable.map((group) => group.id));
  const [returning, setReturning] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [sending, setSending] = useState(false);
  const feedbackInput = useRef(null);
  const respond = async (answer) => {
    if (sending) return;
    setSending(true);
    try { await actions.answer(interaction.id, answer); } finally { setSending(false); }
  };
  return (
    <div className="card card-accent">
      <div className="card-h">确认待修改内容<span className="spacer" />{groups.length} 组</div>
      <div className="card-b">
        {payload.lastSubmissionError && <div className="wb-confirm-submit-error" role="alert">
          <b>刚才没有接受成功</b>
          <span>{userFacingText(payload.lastSubmissionError)}</span>
          <small>候选内容仍然保留；原因处理完成后可再次接受，也可以退回调整。</small>
        </div>}
        {groups.map((group) => {
          const impact = (group.impact || []).filter(Boolean);
          return <label className={`wb-grp ${group.autoApproved ? 'dim' : ''}`} key={group.id}>
            <input type="checkbox" disabled={group.autoApproved || sending}
              checked={group.autoApproved || selected.includes(group.id)}
              onChange={() => setSelected((items) => items.includes(group.id)
                ? items.filter((id) => id !== group.id) : [...items, group.id])} />
            <span><b>{userFacingText(group.title)}</b><span className="gm">{group.tier === 'caliber' ? '业务口径' : group.tier === 'structural' ? '结构变更' : group.tier === 'script' ? '脚本' : '自动同步'}{group.warning ? ` · ${userFacingText(group.warning)}` : ''}</span>
              {impact.length > 0 && <span className="gi" title={impact.join('\n')}>
                涉及 {impact.slice(0, 2).join('、')}{impact.length > 2 ? ` 等 ${impact.length} 处` : ''}
              </span>}
            </span>
          </label>;
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
        </> : <div className="btn-row" style={{ marginTop: 'var(--s-4)' }}>
          <button className="btn btn-p" disabled={sending || selected.length === 0} aria-busy={sending}
            onClick={() => void respond({ approved: true, review_mode: 'full', selected_groups: selected, sampled_groups: [] })}>
            {sending ? '正在接受…' : `接受已选（${selected.length}）`}
          </button>
          {selectable.length > 3 && <button className="btn" disabled={sending}
            onClick={() => {
              const sampled = selected.slice(0, 3);
              void respond({ approved: true, review_mode: 'sampled', selected_groups: selected, sampled_groups: sampled });
            }}>抽检 3 组后接受</button>}
          <button className="btn btn-danger" disabled={sending}
            onClick={() => {
              setReturning(true);
              requestAnimationFrame(() => feedbackInput.current?.focus());
            }}>退回调整</button>
        </div>}
        {policy.find((item) => item.tier === 'structural') && (
          <label className="wb-policy-inline">
            <input type="checkbox"
              checked={policy.find((item) => item.tier === 'structural')?.mode === 'auto'}
              onChange={(event) => void actions.setConfirmPolicy('structural', event.target.checked ? 'auto' : 'ask')} />
            <span><b>后续结构变更不用再问我</b><small>只自动处理对象、操作和指标结构；业务口径与脚本仍必须确认</small></span>
          </label>
        )}
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
    <div className="card card-accent wb-release-confirm">
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
            {['auto', 'goal', 'schema', 'evidence'].map((value) => <option key={value} value={value} disabled={value === 'schema' && !table}>{materialPurposeLabel(value)}</option>)}
          </select></label>
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
