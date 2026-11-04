// 派出的子任务：时间线上一行的卡片（沿用旧版对话页的样式），整张可点，打开进度面板。
// 面板桌面在右侧、窄屏从底部弹出；打开时按 S-21 取全部步骤，之后跟着对话流里的实时更新
import {
  useContext, useEffect, useMemo, useState,
} from 'react';
import { useApp } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { subagentStatus } from '../../state/conversation.js';
import { Drawer, Icon, cx } from '../../ui/index.jsx';
import { WbCtx } from './parts.jsx';
import {
  fmtElapsed, outcome, panelHead, panelSteps, stepTime, thinkingText,
} from './subagentModel.js';

const SUB_TITLE = { done: '子任务完成', failed: '子任务失败', interrupted: '子任务已中断' };
const SUB_ICON = { done: 'check', failed: 'alert', interrupted: 'pause' };
const PAGE = 200;
const PAGES_MAX = 5;

// 第二行：正在想就说想了多久，否则是正在做的那一步；做完了是小结。会话已停下还没收到终态的显示已中断（ADR-0009）
export function SubagentCard({ s, ended = false }) {
  const [open, setOpen] = useState(false);
  const status = subagentStatus(s, ended);
  const over = Boolean(SUB_ICON[status]);
  const line = over ? s.summary
    : s.thinking ? `思考中 · ${fmtElapsed(s.thinking.elapsed_ms)}`
      : (s.progress_text || '独立子代理执行中，主对话稍后汇总结果');
  return (
    <>
      <div
        className={cx('subagent-card', over ? status : 'running')}
        role="button"
        tabIndex={0}
        title="查看子任务进度"
        onClick={() => setOpen(true)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(true); } }}
      >
        <span className="subagent-icon">{over ? <Icon name={SUB_ICON[status]} size={14} /> : <span className="spin" />}</span>
        <span className="subagent-main">
          <span className="subagent-title">{SUB_TITLE[status] || '子任务运行中'} · {s.title}</span>
          {line ? <span className="subagent-hint">{line}</span> : null}
        </span>
        <Icon name="chev" size={14} className="subagent-go" />
      </div>
      <SubagentPanel open={open} onClose={() => setOpen(false)} sub={s} ended={ended} />
    </>
  );
}

// 打开期间每秒走一次表，用来显示已运行多久、思考了多久
function useNow(active) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

function useSubagentSteps(open, taskId) {
  const { api } = useApp();
  const { pid, cid } = useContext(WbCtx);
  const [data, setData] = useState({ task: null, items: [], error: null, loading: false });
  useEffect(() => {
    if (!open || !taskId || !pid || !cid) return undefined;
    let alive = true;
    setData((d) => ({ ...d, loading: true, error: null }));
    (async () => {
      try {
        let cursor = '';
        let task = null;
        const items = [];
        for (let page = 0; page < PAGES_MAX; page += 1) {
          const r = await api.subagentSteps(pid, cid, taskId, { limit: PAGE, ...(cursor ? { cursor } : {}) });
          task = r.task;
          items.push(...(r.items || []));
          cursor = r.next_cursor;
          if (!cursor) break;
        }
        if (alive) setData({ task, items, error: null, loading: false });
      } catch (e) {
        if (alive) setData({ task: null, items: [], error: e, loading: false });
      }
    })();
    return () => { alive = false; };
  }, [api, pid, cid, taskId, open]);
  return data;
}

export function SubagentPanel({
  open, onClose, sub, ended,
}) {
  const fetched = useSubagentSteps(open, sub.task_id);
  const status = subagentStatus(sub, ended);
  const running = !['done', 'failed', 'interrupted'].includes(status);
  const now = useNow(open && running);
  const head = panelHead(sub, fetched.task, ended, now);
  const steps = useMemo(() => panelSteps(fetched.items, sub.steps), [fetched.items, sub.steps]);
  const brief = sub.brief || fetched.task?.brief;
  const end = outcome(head.status, sub.summary || fetched.task?.summary);
  const thinking = running ? thinkingText(sub.thinking, now) : '';
  return (
    <Drawer open={open} onClose={onClose} icon="bot" title={sub.title} sub={head.text} w={460} className="sub-panel">
      {brief ? <Brief text={brief} /> : null}
      <div className="sp-sec">过程</div>
      {fetched.error ? <div className="note red"><Icon name="alert" /><div className="grow">{errorText(fetched.error)}</div></div> : null}
      {!steps.length && !thinking ? (
        <div className="xs muted">{fetched.loading ? '正在取子任务的过程…' : running ? '子任务刚开始，还没有动作' : '这个子任务没有记录到步骤'}</div>
      ) : null}
      <div className="sp-steps">
        {steps.map((step) => <StepRow key={step.id} step={step} now={now} />)}
        {thinking ? (
          <div className="sp-step running">
            <span className="sp-ic"><span className="spin" /></span>
            <span className="sp-tx">{thinking}</span>
          </div>
        ) : null}
      </div>
      {end ? (
        <div className={cx('sp-end', end.tone)}>
          <div className="xs b">{end.label}</div>
          <div className="sm">{end.text}</div>
        </div>
      ) : null}
    </Drawer>
  );
}

function Brief({ text }) {
  const [more, setMore] = useState(false);
  const long = text.length > 120 || text.split('\n').length > 3;
  return (
    <div className="sp-brief">
      <div className="sp-sec">任务说明</div>
      <div className={cx('sp-brief-t', !more && long && 'clamp')}>{text}</div>
      {long ? <button type="button" className="sp-more" onClick={() => setMore(!more)}>{more ? '收起' : '展开'}</button> : null}
    </div>
  );
}

const STEP_ICON = { done: 'check', failed: 'alert' };

function StepRow({ step, now }) {
  if (step.kind === 'text') {
    return <div className="sp-say">{step.text}</div>;
  }
  const time = stepTime(step, now);
  return (
    <div className={cx('sp-step', step.state)}>
      <span className="sp-ic">{STEP_ICON[step.state] ? <Icon name={STEP_ICON[step.state]} size={12} /> : <span className="spin" />}</span>
      <span className="sp-tx">{step.text || '一步操作'}</span>
      {time ? <span className="sp-t">{time}</span> : null}
    </div>
  );
}
