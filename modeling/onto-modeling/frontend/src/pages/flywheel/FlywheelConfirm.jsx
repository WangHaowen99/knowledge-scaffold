import {
  Button, Check, Icon, Modal, Note, Radio, cx,
} from '../../ui/index.jsx';
import {
  confirmsPrompt, earlierChangesNote, feedbackNote, graphPrompt, isFeedback, iteration, nextVersion, version,
} from './flywheelModel.js';

function optimizeContent(run, state) {
  const count = run.suggestions?.count;
  const feedback = isFeedback(run);
  const note = feedbackNote(run, state);
  return {
    title: '确认执行本次优化？',
    label: '确认并优化',
    body: (
      <>
        <p>
          按 {version(run.version)} {feedback ? '用户反馈' : ''}的{count ? ` ${count} 条` : ''}优化建议修订知识内容，执行前保存快照；完成后
          {state.settings.publish ? (feedback ? '检查通过即自动发布，不发起评测。' : '检查通过即自动发布并评测。') : '等待你确认发布。'}
        </p>
        {note ? <p className="muted">{note}</p> : null}
      </>
    ),
  };
}

function publishContent(run, state) {
  const current = state.package?.version;
  const note = earlierChangesNote(run, state);
  return {
    title: '确认发布新版本？',
    label: '确认并发布',
    body: (
      <>
        <div className="fw-target mono">
          <span className="muted">{version(current)}</span><Icon name="arrow" size={12} />
          <b>{nextVersion(current) ? version(nextVersion(current)) : '新正式版本'}</b>
        </div>
        {note ? <Note tone="amber">{note}</Note> : null}
        <p className="muted">
          发布后{isFeedback(run) ? '不发起评测（用户反馈没有评测集）' : state.settings.publish ? '自动评测新版本' : '需你确认才开始评测'}；版本号以 Hub 分配为准。
        </p>
      </>
    ),
  };
}

function evaluateContent(run, state, confirm, onChange) {
  const graph = !!run.graph_confirmation;
  const ticked = confirmsPrompt(confirm.replaceGraph, run);
  return {
    title: '确认评测新发布的版本？',
    label: graph ? '确认删除并开始评测' : '确认并开始评测',
    blocked: graph && !ticked,
    body: (
      <>
        <p>用上一轮的评测集、数据源与模型评测 {version(run.target_version)}；全部用例通过即结束，否则进入下一轮优化。</p>
        {graph ? (
          <>
            <Note tone="amber">{run.graph_confirmation}。</Note>
            <div className={cx('fw-option', ticked && 'on')}>
              <Check on={ticked} onChange={(on) => onChange({ replaceGraph: on ? graphPrompt(run) : null })}
                label={<b>删除评测图范围内的旧数据并重新导入</b>} />
            </div>
          </>
        ) : null}
      </>
    ),
  };
}

function retryContent(run) {
  const renew = ['hub_expired', 'request_conflict'].includes(run.reason);
  return {
    title: '重试本次 Hub 发布？',
    label: '确认重试',
    body: (
      <p>
        {renew ? `为 ${run.id} 的候选内容创建新的发布请求，重试前再次核对内容和检查。`
          : `继续 ${run.id} 的同一发布请求，不会重复创建版本。`}
      </p>
    ),
  };
}

function rollbackContent(run, state, confirm, onChange) {
  const { scope } = confirm;
  const current = state.package?.version;
  const other = state.candidate_id && state.candidate_id !== run.id;
  const option = (value, title, text) => (
    <div className={cx('fw-option', scope === value && 'on')} role="radio" aria-checked={scope === value}
      onClick={() => onChange({ scope: value })}>
      <Radio on={scope === value} label={<b>{title}</b>} />
      <div className="xs muted fw-option-sub">{text}</div>
    </div>
  );
  return {
    title: '回退到本次执行前的内容',
    label: '确认回退',
    danger: true,
    body: (
      <>
        <p>恢复 <b className="mono">{run.id}</b> 开始前的快照 <span className="mono">{iteration(run.snapshot)}</span>。</p>
        {option('workspace', '仅恢复建模内容', `Hub 当前版本 ${version(current)} 保留，需要时再发布。`)}
        {option('hub', '恢复并发布新版本', `检查通过后发布${nextVersion(current) ? ` ${version(nextVersion(current))}` : '新版本'}。`)}
        <Note tone="amber">回退会暂停两个开关{other ? '，并取代当前其他待发布内容' : ''}。</Note>
      </>
    ),
  };
}

const CONTENT = {
  optimize: optimizeContent,
  publish: publishContent,
  retry: retryContent,
  rollback: rollbackContent,
  evaluate: evaluateContent,
  'skip-evaluation': (run) => (run.status === 'evaluating' ? {
    title: '不再等待本次评测？',
    label: '不再等待',
    body: (
      <p>
        评测服务中的这次评测不会被取消；{version(run.target_version)} 记为已发布、未评测，本轮结束。之后评测结果到达时，照常进入数据飞轮。
      </p>
    ),
  } : {
    title: '跳过新版本评测？',
    label: '跳过评测',
    body: <p>{version(run.target_version)} 记为已发布、未评测，本轮结束。之后在评测页面评测，结果照常进入数据飞轮。</p>,
  }),
  stop: (run) => ({
    title: '中止本次优化并恢复快照？',
    label: '中止并恢复',
    danger: true,
    body: (
      <p>
        停止 <b className="mono">{run.id}</b> 并恢复快照 <span className="mono">{iteration(run.snapshot)}</span>；「自动化处理优化建议」开关会暂停。
      </p>
    ),
  }),
  ignore: () => ({
    title: '忽略本次评测建议？',
    label: '忽略本次',
    body: <p>不修改知识包，记录保留。</p>,
  }),
};

// confirm: { kind, run, scope, replaceGraph }；内容按最新同步的运行显示，确认失败后能看到新的提示
export default function FlywheelConfirm({ confirm, state, busy, onChange, onAccept, onCancel }) {
  if (!confirm) return null;
  const run = state?.runs?.find((item) => item.id === confirm.run.id) || confirm.run;
  const content = CONTENT[confirm.kind](run, state, confirm, onChange);
  return (
    <Modal open w={520} icon={content.danger ? 'undo' : 'info'} danger={content.danger} title={content.title} onClose={onCancel}
      foot={(
        <>
          <Button onClick={onCancel}>取消</Button>
          <Button kind={content.danger ? 'danger' : 'primary'} className={content.danger ? 'fill' : undefined}
            disabled={busy || content.blocked} onClick={onAccept}>
            {busy ? <span className="spin" aria-hidden="true" /> : null}{content.label}
          </Button>
        </>
      )}>
      <div className="fw-confirm">{content.body}</div>
    </Modal>
  );
}
