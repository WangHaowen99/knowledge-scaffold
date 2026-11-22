import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import {
  Button, Chip, Field, Icon, KV, Modal, Note, Select, Tabs, cx,
} from '../../ui/index.jsx';
import {
  DELEGATION_OPTIONS, RECHECK_REASONS, RETRY_REASONS, ROUND_LIMITS, canEvaluate, canIgnore, canResume, canRollback,
  canSkipEvaluation, delegationHint, evaluateBlock, evaluationLine, formatDay, formatTime, iteration, optimizeBlock,
  publishBlock, reasonText, reasonTitle, roundsLabel, sourceLine, version,
} from './flywheelModel.js';
import {
  ActionButton, Loading, StatusChip, TextLink,
} from './FlywheelCards.jsx';

// 右侧详情：弹窗的贴边变体，和确认框、报告弹窗同在弹层宿主里按打开先后叠放
function Sheet({ title, sub, onClose, foot, children }) {
  return <Modal open className="fw-side" w="min(560px, 100vw)" title={title} sub={sub} onClose={onClose} foot={foot}>{children}</Modal>;
}

function trigger(run) {
  if (run.kind === 'rollback') return '人工回退';
  const source = sourceLine(run) || '评测通知';
  return `${source} · ${run.mode === 'auto' ? '自动执行' : '人工确认'}`;
}

function outcome(run) {
  if (run.published_release_id || run.status === 'published') {
    return run.local_record_pending ? '已发布，本地发布记录待恢复' : '已收到 Hub 回执';
  }
  if (run.status === 'restored') return '仅恢复建模内容';
  if (run.status === 'passed') return '评测集全部通过，无需发布';
  return '尚未发布新版本';
}

function evaluationText(run) {
  if (run.status === 'waiting_evaluation') return run.human_hold === false ? '等待自动发起' : '等待你确认';
  if (run.status === 'blocked' && run.reason === 'evaluation_failed') return reasonTitle(run);
  return evaluationLine(run) || '—';
}

function Changes({ projectId, run }) {
  const { api } = useApp();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    setData(null);
    setError('');
    api.flywheelChanges(projectId, run.id)
      .then((value) => { if (alive) setData(value); })
      .catch((e) => { if (alive) setError(errorText(e)); });
    return () => { alive = false; };
  }, [api, projectId, run.id, run.status]);
  if (!run.snapshot) return <div className="fw-empty">尚未执行优化</div>;
  if (error) return <Note tone="red" className="fw-block">{error}</Note>;
  if (!data) return <Loading text="正在读取内容差异…" />;
  return (
    <div className="fw-block">
      <div className="sec-t">
        执行前 → {run.status === 'optimizing' ? '当前工作区' : '优化后'} · {data.total} 份文档
        {data.truncated ? `（显示前 ${data.files.length} 份）` : ''}
      </div>
      {run.earlier_changes === true ? (
        <Note tone="amber" className="mb12">上次发布后还有其他未发布的修改，不在此列，发布时会一并发布。</Note>
      ) : null}
      {data.files.length === 0 ? <div className="fw-empty">知识包文档没有变化</div> : null}
      {data.files.map((file) => (
        <div className="fw-diff" key={file.path}>
          <div className="fw-diff-h">
            <Icon name="file" size={14} /><span className="grow ell mono">{file.path}</span>
            <span className="fw-add">+{file.added}</span><span className="fw-del">−{file.removed}</span>
          </div>
          {file.lines.map((line, index) => (
            // eslint-disable-next-line react/no-array-index-key
            <div key={index} className={`fw-line ${line.kind}`}>
              {line.kind === 'added' ? '+ ' : line.kind === 'removed' ? '− ' : ''}{line.text}
            </div>
          ))}
          {file.truncated ? <div className="fw-line hunk">差异较长，其余行请在工作区的历史对比中查看</div> : null}
        </div>
      ))}
    </div>
  );
}

function Checks({ checks }) {
  if (!checks) return null;
  return (
    <div className="fw-block">
      <div className="sec-t">质量与发布检查 · {formatTime(checks.checked_at)}</div>
      {checks.items.map((item) => (
        <div key={item.key} className={cx('fw-check', !item.passed && 'fail')}>
          <Icon name={item.passed ? 'check' : 'alert'} size={14} />
          <span className="grow">
            <b>{item.label}</b> · {item.message}
            {item.details.map((text) => <span key={text} className="fw-sub">{text}</span>)}
          </span>
          <Chip sm tone={item.passed ? 'teal' : 'red'}>{item.passed ? '通过' : '未通过'}</Chip>
        </div>
      ))}
    </div>
  );
}

function footerAction(run, state, onAction) {
  if (canResume(run)) {
    const label = run.status === 'waiting_optimize' ? '确认并优化' : '重新确认优化';
    return <ActionButton kind="primary" reason={optimizeBlock(state, run)} onClick={() => onAction('confirm-optimize', run)}>{label}</ActionButton>;
  }
  if (run.status === 'optimizing' && state.can_write) {
    return <Button onClick={() => onAction('stop', run)}>中止并恢复快照</Button>;
  }
  if (run.status === 'waiting_publish') {
    return <ActionButton kind="primary" reason={publishBlock(state, run)} onClick={() => onAction('confirm-publish', run)}>确认并发布</ActionButton>;
  }
  if (canEvaluate(run)) {
    const label = run.status === 'waiting_evaluation' ? '确认并评测' : '确认重新评测';
    return <ActionButton kind="primary" reason={evaluateBlock(state, run)} onClick={() => onAction('confirm-evaluate', run)}>{label}</ActionButton>;
  }
  if (run.status === 'blocked' && RECHECK_REASONS.includes(run.reason)) {
    return (
      <ActionButton kind="primary" reason={state.can_write ? '' : '只读评审角色不能重新检查'} onClick={() => onAction('recheck', run)}>
        重新检查
      </ActionButton>
    );
  }
  if (run.status === 'blocked' && RETRY_REASONS.includes(run.reason)) {
    return <ActionButton kind="primary" reason={publishBlock(state, run)} onClick={() => onAction('retry-publish', run)}>确认重试发布</ActionButton>;
  }
  if (run.status === 'blocked' && run.reason === 'suggestions_failed') {
    return <Button kind="primary" onClick={() => onAction('suggestions', run)}>查看优化建议</Button>;
  }
  return <Button onClick={() => onAction('close-detail')}>关闭</Button>;
}

export function RunDetail({ projectId, run, state, tab, onTab, onAction, onClose }) {
  const { api } = useApp();
  const [detail, setDetail] = useState(null);
  useEffect(() => {
    let alive = true;
    api.flywheelRun(projectId, run.id).then((value) => { if (alive) setDetail(value); }).catch(() => {});
    return () => { alive = false; };
    // 运行推进时（步骤或状态变化）重新读取执行过程
  }, [api, projectId, run.id, run.status, run.step]);
  const context = { current: state.package?.version, maxRounds: state.settings.max_rounds };
  const pairs = [
    ['触发来源', trigger(run)],
    run.snapshot ? ['执行前快照', <span className="mono">{iteration(run.snapshot)}</span>] : null,
    ['发布结果', outcome(run)],
    run.published_release_id ? ['新版本评测', evaluationText(run)] : null,
  ].filter(Boolean);
  const foot = (
    <>
      {state.can_write && canRollback(run)
        ? <TextLink gray onClick={() => onAction('rollback', run)}><Icon name="undo" size={12} />回退到执行前</TextLink> : null}
      {state.can_write && canSkipEvaluation(run) ? (
        <TextLink gray onClick={() => onAction('skip-evaluation', run)}>
          {run.status === 'evaluating' ? '不再等待评测' : '跳过评测'}
        </TextLink>
      ) : null}
      <span className="sp" />
      {state.can_write && canIgnore(run) ? <Button onClick={() => onAction('ignore', run)}>忽略本次</Button> : null}
      {footerAction(run, state, onAction)}
    </>
  );
  return (
    <Sheet title={<span className="row">{run.id}<StatusChip run={run} /></span>} onClose={onClose} foot={foot}
      sub={`${version(run.content_version || run.version)}${run.target_version ? ` → ${version(run.target_version)}` : ''}`}>
      <KV w="90px" pairs={pairs} />
      <div className="mt16">
        <Tabs items={[['timeline', '执行过程'], ['changes', '内容差异']]} value={tab} onChange={onTab} />
      </div>
      {tab === 'changes' ? <Changes projectId={projectId} run={run} /> : (
        <>
          {!detail ? <Loading text="正在读取执行过程…" /> : (
            <ol className="fw-timeline">
              {detail.history.map((item) => (
                <li key={`${item.at}-${item.title}`} className={item.result === 'done' ? undefined : item.result}>
                  <span className="mono xs muted">{formatTime(item.at)}</span>
                  <div className="b">{item.title}</div>
                  <div className="xs muted">{item.text}</div>
                </li>
              ))}
            </ol>
          )}
          {run.status === 'blocked' ? (
            <Note tone="red" className="fw-block"><b>{reasonTitle(run)}</b><div className="mt4">{reasonText(run, context)}</div></Note>
          ) : null}
          {run.status === 'expired' ? <Note tone="gray" className="fw-block">{reasonText(run, context)}</Note> : null}
          {detail?.error && run.status === 'blocked' ? <p className="xs muted mt8">详情：{detail.error}</p> : null}
          <Checks checks={detail?.checks} />
        </>
      )}
    </Sheet>
  );
}

const ROUND_OPTIONS = ROUND_LIMITS.map((value) => ({ value, label: roundsLabel(value) }));

// 设置：两项选择即时生效
export function SettingsSheet({ state, onChange, onClose }) {
  const { settings } = state;
  const days = settings.delegation_days;
  const granted = state.delegation?.evaluation?.expires_at || state.delegation?.hub?.expires_at;
  const disabled = !state.can_write;
  const pick = (key) => (value) => { if (value !== settings[key]) onChange({ [key]: value }); };
  const rounds = settings.max_rounds ? '达到上限后转为人工确认；评测集全部通过或手动确认一次后重新计数。'
    : '不限轮数：持续优化、发布并评测，直到评测集全部通过；每轮都会发布一个正式版本。';
  const expiry = `${delegationHint(days)}${days !== 0 && granted ? ` 当前到期：${formatDay(granted)}。` : ''}`;
  return (
    <Sheet title="设置" onClose={onClose}>
      <div className="fw-form">
        <Field label="连续自动处理上限" hint={<span className={settings.max_rounds ? undefined : 'fw-warn-text'}>{rounds}</span>}>
          <Select value={settings.max_rounds} options={ROUND_OPTIONS} disabled={disabled} onChange={pick('max_rounds')} />
        </Field>
        <Field label="后台运行有效期" hint={<span className={days === 0 ? 'fw-warn-text' : undefined}>{expiry}</span>}>
          <Select value={days} options={DELEGATION_OPTIONS} disabled={disabled} onChange={pick('delegation_days')} />
        </Field>
      </div>
    </Sheet>
  );
}
