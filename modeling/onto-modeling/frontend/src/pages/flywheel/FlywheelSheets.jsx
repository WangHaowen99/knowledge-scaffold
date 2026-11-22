import { useEffect, useState } from 'react';
import { api } from '../../lib/api.js';
import {
  ACTIVE, DELEGATION_OPTIONS, ROUND_LIMITS, RECHECK_REASONS, RETRY_REASONS, canEvaluate, canIgnore, canResume, canRollback,
  canSkipEvaluation, roundsLabel,
  delegationHint, evaluateBlock, evaluationLine, formatDay, formatTime, iteration, optimizeBlock, publishBlock, reasonText,
  reasonTitle, sourceLine, statusOf, version,
} from '../../lib/flywheel.js';
import { Banner, Button, Link, Tag } from './FlywheelCards.jsx';
import Icon from './Icon.jsx';

export function Sheet({ title, onClose, footer, children }) {
  return (
    <div className="fw-sheet">
      <header className="fw-sheet-header">
        <h2>{title}</h2>
        <span className="fw-spacer" />
        <button type="button" className="fw-icon-button" aria-label="关闭详情" onClick={onClose}><Icon name="close" /></button>
      </header>
      <div className="fw-sheet-body">{children}</div>
      {footer && <footer className="fw-sheet-footer">{footer}</footer>}
    </div>
  );
}

const Loading = ({ text }) => <div className="fw-loading" role="status"><span className="fw-spinner" aria-hidden="true" />{text}</div>;

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
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    setData(null);
    api.flywheelChanges(projectId, run.id)
      .then((value) => { if (alive) setData(value); })
      .catch((e) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [projectId, run.id, run.status]);
  if (!run.snapshot) return <div className="fw-empty">尚未执行优化</div>;
  if (error) return <Banner color="red">{error}</Banner>;
  if (!data) return <Loading text="正在读取内容差异…" />;
  return (
    <div className="fw-block">
      <h3>
        执行前 → {run.status === 'optimizing' ? '当前工作区' : '优化后'} · {data.total} 份文档
        {data.truncated && `（显示前 ${data.files.length} 份）`}
      </h3>
      {run.earlier_changes === true && (
        <div className="fw-warning" style={{ margin: '0 0 12px' }}>上次发布后还有其他未发布的修改，不在此列，发布时会一并发布。</div>
      )}
      {data.files.length === 0 && <div className="fw-empty">知识包文档没有变化</div>}
      {data.files.map((file) => (
        <div className="fw-diff-file" key={file.path}>
          <div className="fw-diff-title"><Icon name="file" size="small" />{file.path}<span>+{file.added} −{file.removed}</span></div>
          {file.lines.map((line, index) => (
            // eslint-disable-next-line react/no-array-index-key
            <div key={index} className={`fw-diff-line ${line.kind}`}>
              {line.kind === 'added' ? '+ ' : line.kind === 'removed' ? '− ' : ''}{line.text}
            </div>
          ))}
          {file.truncated && <div className="fw-diff-line hunk">差异较长，其余行请在工作区的历史对比中查看</div>}
        </div>
      ))}
    </div>
  );
}

function Checks({ checks }) {
  if (!checks) return null;
  return (
    <div className="fw-block">
      <h3>质量与发布检查 · {formatTime(checks.checked_at)}</h3>
      {checks.items.map((item) => (
        <div key={item.key} className={`fw-detail-check ${item.passed ? '' : 'fail'}`}>
          <Icon name={item.passed ? 'check' : 'warning'} />
          <span><strong style={{ fontWeight: 550 }}>{item.label}</strong> · {item.message}
            {item.details.map((text) => <span key={text} className="fw-subline">{text}</span>)}
          </span>
          <Tag color={item.passed ? 'green' : 'red'}>{item.passed ? '通过' : '未通过'}</Tag>
        </div>
      ))}
    </div>
  );
}

function footerAction(run, state, onAction) {
  if (canResume(run)) {
    const label = run.status === 'waiting_optimize' ? '确认并优化' : '重新确认优化';
    return <Button kind="primary" reason={optimizeBlock(state, run)} onClick={() => onAction('confirm-optimize', run)}>{label}</Button>;
  }
  if (run.status === 'optimizing' && state.can_write) {
    return <Button onClick={() => onAction('stop', run)}>中止并恢复快照</Button>;
  }
  if (run.status === 'waiting_publish') {
    return <Button kind="primary" reason={publishBlock(state, run)} onClick={() => onAction('confirm-publish', run)}>确认并发布</Button>;
  }
  if (canEvaluate(run)) {
    const label = run.status === 'waiting_evaluation' ? '确认并评测' : '确认重新评测';
    return <Button kind="primary" reason={evaluateBlock(state, run)} onClick={() => onAction('confirm-evaluate', run)}>{label}</Button>;
  }
  if (run.status === 'blocked' && RECHECK_REASONS.includes(run.reason)) {
    return <Button kind="primary" reason={state.can_write ? '' : '只读评审角色不能重新检查'} onClick={() => onAction('recheck', run)}>重新检查</Button>;
  }
  if (run.status === 'blocked' && RETRY_REASONS.includes(run.reason)) {
    return <Button kind="primary" reason={publishBlock(state, run)} onClick={() => onAction('retry-publish', run)}>确认重试发布</Button>;
  }
  if (run.status === 'blocked' && run.reason === 'suggestions_failed') {
    return <Button kind="primary" onClick={() => onAction('suggestions', run)}>查看优化建议</Button>;
  }
  return <Button onClick={() => onAction('close-detail')}>关闭</Button>;
}

export function RunDetail({ projectId, run, state, tab, onTab, onAction }) {
  const [detail, setDetail] = useState(null);
  useEffect(() => {
    let alive = true;
    api.flywheelRun(projectId, run.id).then((value) => { if (alive) setDetail(value); }).catch(() => {});
    return () => { alive = false; };
    // 运行推进时（步骤或状态变化）重新读取执行过程
  }, [projectId, run.id, run.status, run.step]);
  const [label, color] = statusOf(run);
  const context = { current: state.package?.version, maxRounds: state.settings.max_rounds };
  const footer = (
    <>
      <span className="fw-row">
        {state.can_write && canRollback(run)
          && <Link gray onClick={() => onAction('rollback', run)}><Icon name="rollback" size="small" />回退到执行前</Link>}
        {state.can_write && canSkipEvaluation(run) && (
          <Link gray onClick={() => onAction('skip-evaluation', run)}>
            {run.status === 'evaluating' ? '不再等待评测' : '跳过评测'}
          </Link>
        )}
      </span>
      {/* 忽略放在主操作左侧，和关闭、确认在一起，一眼能看到 */}
      <span className="fw-row">
        {state.can_write && canIgnore(run) && <Button onClick={() => onAction('ignore', run)}>忽略本次</Button>}
        {footerAction(run, state, onAction)}
      </span>
    </>
  );
  return (
    <Sheet title={(
      <span className="fw-row">
        {run.id}
        <Tag color={color} live={ACTIVE.includes(run.status)}>{label}</Tag>
        <span className="fw-muted fw-mono fw-sheet-version">
          {version(run.content_version || run.version)}{run.target_version ? ` → ${version(run.target_version)}` : ''}
        </span>
      </span>
    )} onClose={() => onAction('close-detail')} footer={footer}>
      <div className="fw-block">
        <div className="fw-meta">
          <div><label>触发来源</label>{trigger(run)}</div>
          {run.snapshot && <div><label>执行前快照</label><span className="fw-mono">{iteration(run.snapshot)}</span></div>}
          <div><label>发布结果</label>{outcome(run)}</div>
          {run.published_release_id && <div><label>新版本评测</label>{evaluationText(run)}</div>}
        </div>
      </div>
      <div className="fw-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'timeline'} className={tab === 'timeline' ? 'active' : ''} onClick={() => onTab('timeline')}>执行过程</button>
        <button type="button" role="tab" aria-selected={tab === 'changes'} className={tab === 'changes' ? 'active' : ''} onClick={() => onTab('changes')}>内容差异</button>
      </div>
      {tab === 'changes' ? <Changes projectId={projectId} run={run} /> : (
        <>
          {!detail ? <Loading text="正在读取执行过程…" /> : (
            <ol className="fw-timeline">
              {detail.history.map((item) => (
                <li key={`${item.at}-${item.title}`} className={item.result === 'done' ? '' : item.result}>
                  <span className="fw-timeline-time fw-mono">{formatTime(item.at)}</span>
                  <strong style={{ fontWeight: 550 }}>{item.title}</strong>
                  <div className="fw-timeline-text">{item.text}</div>
                </li>
              ))}
            </ol>
          )}
          {run.status === 'blocked' && (
            <div style={{ marginTop: 22 }}>
              <Banner color="red" extra={reasonText(run, context)}><strong>{reasonTitle(run)}</strong></Banner>
            </div>
          )}
          {run.status === 'expired' && (
            <div style={{ marginTop: 22 }}><Banner color="neutral">{reasonText(run, context)}</Banner></div>
          )}
          {detail?.error && run.status === 'blocked' && <p className="fw-detail-text">详情：{detail.error}</p>}
          <Checks checks={detail?.checks} />
        </>
      )}
    </Sheet>
  );
}

// 设置：两项选择即时生效；关于后台授权只留一句必须知道的
export function SettingsSheet({ state, onChange, onClose }) {
  const { settings } = state;
  const days = settings.delegation_days;
  const granted = state.delegation?.evaluation?.expires_at || state.delegation?.hub?.expires_at;
  const disabled = !state.can_write;
  return (
    <Sheet title="设置" onClose={onClose}>
      <label className="fw-form-field">
        <span className="fw-form-label">连续自动处理上限</span>
        <select value={settings.max_rounds} disabled={disabled}
          onChange={(event) => onChange({ max_rounds: Number(event.target.value) })}>
          {ROUND_LIMITS.map((value) => <option key={value} value={value}>{roundsLabel(value)}</option>)}
        </select>
        <p className={`fw-form-hint ${settings.max_rounds ? '' : 'warn'}`}>
          {settings.max_rounds ? '达到上限后转为人工确认；评测集全部通过或手动确认一次后重新计数。'
            : '不限轮数：持续优化、发布并评测，直到评测集全部通过；每轮都会发布一个正式版本。'}
        </p>
      </label>
      <label className="fw-form-field">
        <span className="fw-form-label">后台运行有效期</span>
        <select value={days} disabled={disabled} onChange={(event) => onChange({ delegation_days: Number(event.target.value) })}>
          {DELEGATION_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
        <p className={`fw-form-hint ${days === 0 ? 'warn' : ''}`}>
          {delegationHint(days)}{days !== 0 && granted ? ` 当前到期：${formatDay(granted)}。` : ''}
        </p>
      </label>
      <p className="fw-form-hint">
        开关开启时在评测服务与 Hub 登记后台授权，关闭即销毁；修改密码不会结束授权。评测服务另存一份后台评测登录用于自动发起评测：在任一设备退出登录或修改密码后它即作废，需在这里重新授权；它被删除后，副本仍在其有效期内有效。
      </p>
    </Sheet>
  );
}
