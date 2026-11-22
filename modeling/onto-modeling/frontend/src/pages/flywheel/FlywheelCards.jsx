import {
  Button, Chip, Icon, Note, Toggle, cx,
} from '../../ui/index.jsx';
import {
  ACTIVE, EVALUATE_STEP, EVALUATE_STEPS, EVALUATION_REASONS, OPTIMIZE_STEPS, PUBLISH_STEPS, RECHECK_REASONS, canRollback,
  chipTone, earlierChangesNote, evaluateBlock, evaluationHint, feedbackNote, holdHint, isFeedback, loginMissing, nextVersion,
  optimizeBlock, optimizeFocus, publishBlock, publishFocus, rate, reasonText, reasonTitle, statusOf, switchWarning, version,
} from './flywheelModel.js';

export const TextLink = ({ gray = false, className, children, ...props }) => (
  <button type="button" className={cx('fw-link', gray && 'gray', className)} {...props}>{children}</button>
);

// 不能执行时置灰，悬停看原因
export const ActionButton = ({ reason = '', disabled, ...props }) => (
  <Button disabled={!!reason || disabled} title={reason || undefined} {...props} />
);

export function StatusChip({ run }) {
  const [label, color] = statusOf(run);
  const live = ACTIVE.includes(run.status);
  return <Chip sm tone={chipTone(color)} dot={live} className={live ? 'fw-live' : undefined}>{label}</Chip>;
}

export const Loading = ({ text }) => <div className="fw-loading" role="status"><span className="spin" aria-hidden="true" />{text}</div>;

// 卡片头：标题、一条关键事实与开关；开关有问题时下面多一行提示
function CardHeading({ title, meta, checked, disabled, onToggle, warning }) {
  return (
    <>
      <div className="card-h">
        <h3>{title}</h3>
        {meta ? <span className="xs muted">{meta}</span> : null}
        <span className="sp" />
        <Toggle on={checked} disabled={disabled} title={title} onChange={onToggle} />
      </div>
      {warning ? <Note tone="amber" className="fw-card-note">{warning}</Note> : null}
    </>
  );
}

function Steps({ labels, index }) {
  return (
    <ol className="fw-steps">
      {labels.map((text, i) => (
        <li key={text} className={cx(i < index && 'done', i === index && 'cur')}>
          <i>{i < index ? <Icon name="check" size={12} /> : i + 1}</i>{text}
        </li>
      ))}
    </ol>
  );
}

function Running({ title, labels, index, children }) {
  return (
    <div>
      <div className="row g12">
        <span className="spin" aria-hidden="true" /><span className="b">{title}</span>
        <span className="sp" />
        {children}
      </div>
      <Steps labels={labels} index={index} />
    </div>
  );
}

function suggestionText(run) {
  const { count, categories = [] } = run.suggestions || {};
  if (!count) return '优化建议已就绪';
  return `${count} 条建议待优化${categories.length ? `，涉及${categories.join('、')}` : ''}`;
}

function PendingOptimize({ run, state, onAction }) {
  const queued = run.human_hold === false;
  const feedback = isFeedback(run);
  const note = feedbackNote(run, state);
  return (
    <div className="fw-pending">
      <div className="row">
        <Chip sm tone={queued ? 'blue' : 'amber'}>{queued ? '等待自动执行' : '待确认'}</Chip>
        <span className="b">{version(run.version)} {feedback ? '用户反馈' : '评测完成'}</span>
      </div>
      <div className="fw-summary">
        <div className="fw-score">
          <span className="num">{feedback ? run.cases?.total ?? '—' : rate(run.score)}</span>
          <span className="xs muted">{feedback ? '反馈问题' : `严格通过率${run.cases ? ` · ${run.cases.pass}/${run.cases.total}` : ''}`}</span>
        </div>
        <span>{suggestionText(run)}</span>
      </div>
      {note ? <p className="xs muted mb8">{note}</p> : null}
      <div className="row g12">
        <TextLink onClick={() => onAction('suggestions', run)}>优化建议</TextLink>
        <TextLink onClick={() => onAction('report', run)}>{feedback ? '反馈报告' : '评测报告'}</TextLink>
        <span className="sp" />
        {!queued && state.can_write ? <TextLink gray onClick={() => onAction('ignore', run)}>忽略</TextLink> : null}
        {!queued ? (
          <ActionButton kind="primary" size="sm" reason={optimizeBlock(state, run)} onClick={() => onAction('confirm-optimize', run)}>
            确认并优化
          </ActionButton>
        ) : null}
      </div>
    </div>
  );
}

// 没有待处理事项时的一行：说明现状，附最近一条的入口
function Idle({ icon = 'check', text, link }) {
  return (
    <div className="fw-idle">
      <Icon name={icon} size={14} />
      <span>{text}</span>
      <span className="sp" />
      {link}
    </div>
  );
}

export function OptimizeCard({ state, onAction }) {
  const { settings } = state;
  const focus = optimizeFocus(state.runs);
  const run = focus.run;
  let body;
  if (focus.kind === 'running') {
    body = (
      <Running title="正在优化" labels={OPTIMIZE_STEPS} index={Math.min(run.step || 0, OPTIMIZE_STEPS.length - 1)}>
        <TextLink onClick={() => onAction('chat', run)}>助手对话</TextLink>
        <TextLink onClick={() => onAction('detail', run)}>详情</TextLink>
        {state.can_write ? <TextLink gray onClick={() => onAction('stop', run)}>中止并恢复</TextLink> : null}
      </Running>
    );
  } else if (focus.kind === 'waiting') {
    const hint = settings.optimize ? holdHint(run, settings.max_rounds) : '';
    body = (
      <>
        <PendingOptimize run={run} state={state} onAction={onAction} />
        {hint || focus.more > 0 ? (
          <div className="fw-more">
            <span>{hint}{focus.more > 0 ? `${hint ? ' ' : ''}另有 ${focus.more} 条排队。` : ''}</span>
            {focus.more > 0 ? <TextLink onClick={() => onAction('filter-pending')}>查看全部</TextLink> : null}
          </div>
        ) : null}
      </>
    );
  } else if (focus.kind === 'blocked') {
    const suggestions = run.reason === 'suggestions_failed';
    body = (
      <Note tone="red">
        <b>{reasonTitle(run)}</b>
        <div className="mt4">
          {reasonText(run, { current: state.package?.version, maxRounds: settings.max_rounds })}{' '}
          <TextLink onClick={() => onAction(suggestions ? 'suggestions' : 'detail', run)}>{suggestions ? '优化建议' : '处理'}</TextLink>
        </div>
      </Note>
    );
  } else if (focus.kind === 'passed') {
    body = <Idle text={`${version(run.version)} 评测集全部通过，无需优化`}
      link={<TextLink onClick={() => onAction('report', run)}>评测报告</TextLink>} />;
  } else {
    body = <Idle icon={run ? 'check' : 'bell'} text={state.evaluation.evaluating ? '当前版本评测进行中' : '暂无待处理的优化建议'}
      link={run ? <TextLink onClick={() => onAction('changes', run)}>最近一次 {run.id}</TextLink> : null} />;
  }
  return (
    <section className={cx('card fw-card', focus.kind === 'running' && 'running')} aria-label="自动化处理优化建议">
      <CardHeading title="自动化处理优化建议" meta={settings.max_rounds ? `最多连续 ${settings.max_rounds} 轮` : '不限轮数'}
        checked={settings.optimize} disabled={!state.can_write} onToggle={() => onAction('toggle', 'optimize')}
        warning={switchWarning(state, 'optimize')} />
      <div className="card-b">{body}</div>
    </section>
  );
}

function PendingPublish({ run, state, onAction }) {
  const current = state.package?.version;
  const note = earlierChangesNote(run, state);
  return (
    <div className="fw-pending">
      <div className="row g12">
        <Chip sm tone="amber">待确认发布</Chip>
        <span className="row g4 mono">
          <span className="muted">{version(current)}</span><Icon name="arrow" size={12} />
          <b>{nextVersion(current) ? version(nextVersion(current)) : '新版本'}</b>
        </span>
        <span className="muted">{run.kind === 'rollback' ? '恢复的建模内容' : `${run.changed ?? 0} 份文档已更新`}</span>
        <span className="sp" />
        <TextLink onClick={() => onAction('changes', run)}>对比改动</TextLink>
        <ActionButton kind="primary" size="sm" reason={publishBlock(state, run)} onClick={() => onAction('confirm-publish', run)}>
          确认并发布
        </ActionButton>
      </div>
      {note ? <Note tone="amber" className="mt8">{note}</Note> : null}
    </div>
  );
}

function PendingEvaluation({ run, state, onAction }) {
  const queued = run.human_hold === false;
  const hint = evaluationHint(run, state);
  return (
    <div className="fw-pending">
      <div className="row g12">
        <Chip sm tone={queued ? 'blue' : 'amber'}>{queued ? '等待自动评测' : '待确认评测'}</Chip>
        <span className="b">{version(run.target_version)} 已发布</span>
        <span className="sp" />
        {!queued && state.can_write ? <TextLink gray onClick={() => onAction('skip-evaluation', run)}>跳过</TextLink> : null}
        {!queued ? (
          <ActionButton kind="primary" size="sm" reason={evaluateBlock(state, run)} onClick={() => onAction('confirm-evaluate', run)}>
            确认并评测
          </ActionButton>
        ) : null}
      </div>
      {hint ? <p className="xs muted mt8">{hint}</p> : null}
    </div>
  );
}

function PublishBlocked({ run, state, onAction }) {
  const recheck = RECHECK_REASONS.includes(run.reason);
  const evaluate = EVALUATION_REASONS.includes(run.reason);
  const [label, action, reason] = recheck ? ['查看检查结果', 'detail', '']
    : evaluate ? ['重新评测', 'confirm-evaluate', evaluateBlock(state, run)]
      : ['重试发布', 'retry-publish', publishBlock(state, run)];
  return (
    <div className="fw-pending">
      <div className="row g12">
        <Chip sm tone="red">{reasonTitle(run)}</Chip>
        <span className="sp" />
        <TextLink onClick={() => onAction('detail', run)}>详情</TextLink>
        {state.can_write && evaluate ? <TextLink gray onClick={() => onAction('skip-evaluation', run)}>跳过</TextLink> : null}
        {state.can_write && !evaluate && canRollback(run)
          ? <TextLink gray onClick={() => onAction('rollback', run)}>恢复优化前内容</TextLink> : null}
        <ActionButton size="sm" reason={reason} onClick={() => onAction(action, run)}>{label}</ActionButton>
      </div>
      <p className="xs muted mt8">{reasonText(run, { current: state.package?.version })}</p>
    </div>
  );
}

function publishIdle(latest, state) {
  if (state.runs.some((run) => run.status === 'optimizing')) return ['clock', '等待上方优化完成'];
  const released = latest && latest.target_version === state.package?.version;
  if (released && latest.evaluation?.passed) return ['check', `${version(latest.target_version)} 评测集全部通过，循环结束`];
  return ['pkg', '暂无待发布内容'];
}

export function PublishCard({ state, onAction }) {
  const { settings } = state;
  const focus = publishFocus(state.runs, state.package?.release_id);
  const run = focus.run;
  const running = focus.kind === 'running';
  let body;
  if (running && run.status === 'evaluating') {
    body = (
      <Running title={`正在评测 ${version(run.target_version)}`} labels={EVALUATE_STEPS} index={EVALUATE_STEP}>
        {state.can_write ? <TextLink gray onClick={() => onAction('skip-evaluation', run)}>不再等待</TextLink> : null}
        <TextLink onClick={() => onAction('detail', run)}>详情</TextLink>
      </Running>
    );
  } else if (running) {
    body = (
      <Running title="正在发布到 Hub" labels={PUBLISH_STEPS} index={Math.min(run.step || 0, PUBLISH_STEPS.length - 1)}>
        <TextLink onClick={() => onAction('detail', run)}>详情</TextLink>
      </Running>
    );
  } else if (focus.kind === 'waiting') body = <PendingPublish run={run} state={state} onAction={onAction} />;
  else if (focus.kind === 'evaluate') body = <PendingEvaluation run={run} state={state} onAction={onAction} />;
  else if (focus.kind === 'blocked') body = <PublishBlocked run={run} state={state} onAction={onAction} />;
  else {
    const [icon, text] = publishIdle(run, state);
    body = <Idle icon={icon} text={text} link={run ? <TextLink onClick={() => onAction('detail', run)}>最近发布 {run.id}</TextLink> : null} />;
  }
  const warning = switchWarning(state, 'publish');
  return (
    <section className={cx('card fw-card', running && 'running')} aria-label="自动化确认发布与发起评测">
      <CardHeading title="自动化确认发布与发起评测" checked={settings.publish} disabled={!state.can_write}
        onToggle={() => onAction('toggle', 'publish')}
        warning={warning ? (
          <>{warning}{state.can_write && loginMissing(state)
            ? <> <TextLink onClick={() => onAction('reauthorize')}>重新授权</TextLink></> : null}</>
        ) : null} />
      <div className="card-b">{body}</div>
    </section>
  );
}
