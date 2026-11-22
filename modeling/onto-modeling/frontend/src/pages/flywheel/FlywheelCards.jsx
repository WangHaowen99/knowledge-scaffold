import {
  EVALUATE_STEP, EVALUATE_STEPS, EVALUATION_REASONS, OPTIMIZE_STEPS, PUBLISH_STEPS, RECHECK_REASONS, canRollback,
  earlierChangesNote, evaluateBlock, evaluationHint, feedbackNote, holdHint, isFeedback, loginMissing, nextVersion,
  optimizeBlock, optimizeFocus, publishBlock, publishFocus, rate, reasonText, reasonTitle, switchWarning, version,
} from '../../lib/flywheel.js';
import Icon from './Icon.jsx';

export const Tag = ({ color = 'gray', live = false, children }) => (
  <span className={`fw-tag ${color}`}>{live && <span className="fw-dot pulse" />}{children}</span>
);

export const Link = ({ gray = false, children, ...props }) => (
  <button type="button" className={`fw-link ${gray ? 'gray' : ''}`} {...props}>{children}</button>
);

export const Button = ({ kind = '', reason = '', children, ...props }) => (
  <button type="button" className={`fw-button ${kind}`} disabled={!!reason || props.disabled}
    title={reason || undefined} {...props}>{children}</button>
);

const BANNER_ICONS = { amber: 'clock', green: 'circle-check', red: 'warning' };

export function Banner({ color, children, extra, id }) {
  return (
    <div className={`fw-banner ${color}`} id={id}>
      <Icon name={BANNER_ICONS[color] || 'info'} />
      <div>{children}{extra && <div className="fw-banner-extra">{extra}</div>}</div>
    </div>
  );
}

// 卡片头：序号、标题、一条关键事实与开关；开关有问题时下面多一行提示
function CardHeading({ number, publish = false, title, meta, label, checked, disabled, onToggle, warning }) {
  return (
    <>
      <div className="fw-card-heading">
        <div className={`fw-stage-number ${publish ? 'publish' : ''}`}>{number}</div>
        <h2 className="fw-card-title">{title}</h2>
        <span className="fw-card-meta">{meta}</span>
        <span className="fw-spacer" />
        <button type="button" className="fw-switch" role="switch" aria-checked={checked} aria-label={label}
          disabled={disabled} onClick={onToggle} />
      </div>
      {warning && <div className="fw-card-warning"><Icon name="warning" size="small" /><span>{warning}</span></div>}
    </>
  );
}

function Steps({ labels, index }) {
  return (
    <div className="fw-run-steps">
      {labels.map((text, i) => (
        <div key={text} className={`fw-run-step ${i < index ? 'done' : i === index ? 'current' : ''}`}>
          <span className="fw-step-circle">{i < index ? '✓' : i + 1}</span>{text}
        </div>
      ))}
    </div>
  );
}

function Running({ title, run, labels, index, children }) {
  return (
    <div className="fw-run">
      <div className="fw-run-heading">
        <span className="fw-spinner" aria-hidden="true" />{title}
        <span className="fw-muted fw-mono">{run.id}</span>
        <span className="fw-spacer" />
        <div className="fw-row">{children}</div>
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
      <div className="fw-pending-top">
        <Tag color={queued ? 'blue' : 'amber'}>{queued ? '等待自动执行' : '待确认'}</Tag>
        <strong>{version(run.version)} {feedback ? '用户反馈' : '评测完成'}</strong>
        <span className="fw-muted fw-mono">{run.id}</span>
      </div>
      <div className="fw-feedback-body">
        <div className="fw-score">
          {feedback ? (
            <>
              <span className="fw-score-number">{run.cases?.total ?? '—'}</span>
              <div className="fw-score-label">反馈问题</div>
            </>
          ) : (
            <>
              <span className="fw-score-number">{rate(run.score)}</span>
              <div className="fw-score-label">严格通过率{run.cases ? ` · ${run.cases.pass}/${run.cases.total}` : ''}</div>
            </>
          )}
        </div>
        <div className="fw-suggestion-summary">{suggestionText(run)}</div>
      </div>
      {note && <p className="fw-pending-hint">{note}</p>}
      <div className="fw-feedback-bottom">
        <div className="fw-row">
          <Link onClick={() => onAction('suggestions', run)}>优化建议</Link>
          <Link onClick={() => onAction('report', run)}>{feedback ? '反馈报告' : '评测报告'}</Link>
        </div>
        {!queued && (
          <div className="fw-pending-actions">
            {state.can_write && <Link gray onClick={() => onAction('ignore', run)}>忽略</Link>}
            <Button kind="primary compact" reason={optimizeBlock(state, run)} onClick={() => onAction('confirm-optimize', run)}>
              确认并优化
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

// 没有待处理事项时的一行：说明现状，附最近一条的入口
function Idle({ icon = 'circle-check', text, link }) {
  return (
    <div className="fw-idle">
      <Icon name={icon} />
      <span>{text}</span>
      <span className="fw-spacer" />
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
      <Running title="正在优化" run={run} labels={OPTIMIZE_STEPS} index={Math.min(run.step || 0, OPTIMIZE_STEPS.length - 1)}>
        <Link onClick={() => onAction('chat', run)}>助手对话</Link>
        <Link onClick={() => onAction('detail', run)}>详情</Link>
        {state.can_write && <Link gray onClick={() => onAction('stop', run)}>中止并恢复</Link>}
      </Running>
    );
  } else if (focus.kind === 'waiting') {
    const hint = settings.optimize ? holdHint(run, settings.max_rounds) : '';
    body = (
      <>
        <PendingOptimize run={run} state={state} onAction={onAction} />
        {(hint || focus.more > 0) && (
          <div className="fw-more">
            <span>{hint}{focus.more > 0 && `${hint ? ' ' : ''}另有 ${focus.more} 条排队。`}</span>
            {focus.more > 0 && <Link onClick={() => onAction('filter-pending')}>查看全部</Link>}
          </div>
        )}
      </>
    );
  } else if (focus.kind === 'blocked') {
    const suggestions = run.reason === 'suggestions_failed';
    body = (
      <Banner color="red" extra={(
        <>{reasonText(run, { current: state.package?.version, maxRounds: settings.max_rounds })}{' '}
          <Link onClick={() => onAction(suggestions ? 'suggestions' : 'detail', run)}>{suggestions ? '优化建议' : '处理'}</Link></>
      )}>
        <strong>{reasonTitle(run)}</strong> <span className="fw-mono">· {run.id}</span>
      </Banner>
    );
  } else if (focus.kind === 'passed') {
    body = <Idle text={`${version(run.version)} 评测集全部通过，无需优化`}
      link={<Link onClick={() => onAction('report', run)}>评测报告</Link>} />;
  } else {
    body = <Idle icon={run ? 'circle-check' : 'bell'} text={state.evaluation.evaluating ? '当前版本评测进行中' : '暂无待处理的优化建议'}
      link={run && <Link onClick={() => onAction('changes', run)}>最近一次 {run.id}</Link>} />;
  }
  return (
    <section className={`fw-card ${focus.kind === 'running' ? 'running' : ''}`} aria-labelledby="fw-optimize-heading">
      <CardHeading number="01" title={<span id="fw-optimize-heading">自动化处理优化建议</span>}
        meta={settings.max_rounds ? `最多连续 ${settings.max_rounds} 轮` : '连续处理不限轮数'}
        label="自动化处理优化建议" checked={settings.optimize}
        disabled={!state.can_write} onToggle={() => onAction('toggle', 'optimize')} warning={switchWarning(state, 'optimize')} />
      <div className="fw-stage-content">{body}</div>
    </section>
  );
}

function PendingPublish({ run, state, onAction }) {
  const current = state.package?.version;
  const note = earlierChangesNote(run, state);
  return (
    <div className="fw-pending">
      <div className="fw-pending-top">
        <Tag color="amber">待确认发布</Tag>
        <span className="fw-muted fw-mono">{run.id}</span>
      </div>
      <div className="fw-candidate-row">
        <div className="fw-candidate-versions">
          <span className="fw-version-tile old fw-mono">{version(current)}</span>
          <Icon name="arrow" size="small" />
          <span className="fw-version-tile fw-mono">{nextVersion(current) ? version(nextVersion(current)) : '新版本'}</span>
          <span className="fw-muted">{run.kind === 'rollback' ? '恢复的建模内容' : `${run.changed ?? 0} 份文档已更新`}</span>
        </div>
        <div className="fw-pending-actions">
          <Link onClick={() => onAction('changes', run)}>对比改动</Link>
          <Button kind="primary compact" reason={publishBlock(state, run)} onClick={() => onAction('confirm-publish', run)}>
            确认并发布
          </Button>
        </div>
      </div>
      {note && <div className="fw-warning">{note}</div>}
    </div>
  );
}

function PendingEvaluation({ run, state, onAction }) {
  const queued = run.human_hold === false;
  const hint = evaluationHint(run, state);
  return (
    <div className="fw-pending">
      <div className="fw-pending-top">
        <Tag color={queued ? 'blue' : 'amber'}>{queued ? '等待自动评测' : '待确认评测'}</Tag>
        <strong>{version(run.target_version)} 已发布</strong>
        <span className="fw-muted fw-mono">{run.id}</span>
        <span className="fw-spacer" />
        {!queued && (
          <div className="fw-pending-actions">
            {state.can_write && <Link gray onClick={() => onAction('skip-evaluation', run)}>跳过</Link>}
            <Button kind="primary compact" reason={evaluateBlock(state, run)} onClick={() => onAction('confirm-evaluate', run)}>
              确认并评测
            </Button>
          </div>
        )}
      </div>
      {hint && <p className="fw-pending-hint">{hint}</p>}
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
      <div className="fw-pending-top">
        <Tag color="red">{reasonTitle(run)}</Tag>
        <span className="fw-muted fw-mono">{run.id}</span>
        <span className="fw-spacer" />
        <div className="fw-pending-actions">
          <Link onClick={() => onAction('detail', run)}>详情</Link>
          {state.can_write && evaluate && <Link gray onClick={() => onAction('skip-evaluation', run)}>跳过</Link>}
          {state.can_write && !evaluate && canRollback(run) && <Link gray onClick={() => onAction('rollback', run)}>恢复优化前内容</Link>}
          <Button kind="compact" reason={reason} onClick={() => onAction(action, run)}>{label}</Button>
        </div>
      </div>
      <p className="fw-pending-hint">{reasonText(run, { current: state.package?.version })}</p>
    </div>
  );
}

function publishIdle(latest, state) {
  if (state.runs.some((run) => run.status === 'optimizing')) return ['clock', '等待上方优化完成'];
  const released = latest && latest.target_version === state.package?.version;
  if (released && latest.evaluation?.passed) return ['circle-check', `${version(latest.target_version)} 评测集全部通过，循环结束`];
  return ['package', '暂无待发布内容'];
}

export function PublishCard({ state, onAction }) {
  const { settings } = state;
  const focus = publishFocus(state.runs, state.package?.release_id);
  const run = focus.run;
  const running = focus.kind === 'running';
  let body;
  if (running && run.status === 'evaluating') {
    body = (
      <Running title={`正在评测 ${version(run.target_version)}`} run={run} labels={EVALUATE_STEPS} index={EVALUATE_STEP}>
        {state.can_write && <Link gray onClick={() => onAction('skip-evaluation', run)}>不再等待</Link>}
        <Link onClick={() => onAction('detail', run)}>详情</Link>
      </Running>
    );
  } else if (running) {
    body = (
      <Running title="正在发布到 Hub" run={run} labels={PUBLISH_STEPS} index={Math.min(run.step || 0, PUBLISH_STEPS.length - 1)}>
        <Link onClick={() => onAction('detail', run)}>详情</Link>
      </Running>
    );
  } else if (focus.kind === 'waiting') body = <PendingPublish run={run} state={state} onAction={onAction} />;
  else if (focus.kind === 'evaluate') body = <PendingEvaluation run={run} state={state} onAction={onAction} />;
  else if (focus.kind === 'blocked') body = <PublishBlocked run={run} state={state} onAction={onAction} />;
  else {
    const [icon, text] = publishIdle(run, state);
    body = <Idle icon={icon} text={text} link={run && <Link onClick={() => onAction('detail', run)}>最近发布 {run.id}</Link>} />;
  }
  const warning = switchWarning(state, 'publish');
  return (
    <section className={`fw-card ${running ? 'running' : ''}`} aria-labelledby="fw-publish-heading">
      <CardHeading number="02" publish title={<span id="fw-publish-heading">自动化确认发布与发起评测</span>}
        meta="循环至评测集全部通过" label="自动化确认发布与发起评测" checked={settings.publish}
        disabled={!state.can_write} onToggle={() => onAction('toggle', 'publish')}
        warning={warning && (
          <>{warning}{state.can_write && loginMissing(state) && <> <Link onClick={() => onAction('reauthorize')}>重新授权</Link></>}</>
        )} />
      <div className="fw-stage-content">{body}</div>
    </section>
  );
}
