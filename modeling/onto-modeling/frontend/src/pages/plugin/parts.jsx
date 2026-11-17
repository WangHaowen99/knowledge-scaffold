// 插件页与资料页共用件（设计稿 pages-40-more.js 的 mrPlg / mrPlgD / mrSend / mrRun、mr-lock）：
// 页头插件标识、「由某能力包提供」说明、发命令按钮、进度条、写入范围只读条、插件未启用时的空状态。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  finishedTasks, isTaskActive, mergeTask, pageTask, sendHint, taskDetail, taskPercent,
} from '../../state/commandTask.js';
import { isLocked, lockState } from '../../state/readonly.js';
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { Button, Empty, Icon, cx } from '../../ui/index.jsx';
import '../../styles/more.css';

// 订阅项目流里的某几类事件（artifacts.updated、release.updated…）
export function useProjectEvent(types, handler) {
  const { subscribe } = useProject();
  const ref = useRef(handler);
  ref.current = handler;
  const key = [].concat(types).join(',');
  useEffect(() => subscribe((env) => {
    if (key.split(',').includes(env.type)) ref.current(env.data || {}, env);
  }), [subscribe, key]);
}

/**
 * 这一页发出的命令任务：进页面时用 S-19 恢复进行中的任务，项目流 command.progress 实时更新。
 * onFinish(task)：任务从进行中变成结束时调用（刷新列表、提示结果）。
 */
export function useCommandTasks(page, { onFinish } = {}) {
  const { api } = useApp();
  const { pid } = useProject();
  const [tasks, setTasks] = useState([]);
  const [sending, setSending] = useState(false);
  const finishRef = useRef(onFinish);
  finishRef.current = onFinish;
  const listRef = useRef([]);
  const apply = useCallback((fn) => {
    const prev = listRef.current;
    const next = fn(prev);
    listRef.current = next;
    setTasks(next);
    finishedTasks(prev, next).forEach((t) => finishRef.current?.(t));
  }, []);

  useEffect(() => {
    let alive = true;
    api.commandTasks(pid, { origin_page: page }).then((r) => {
      if (alive) apply((prev) => (r.items || []).reduce(mergeTask, prev));
    }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid, page, apply]);

  useProjectEvent('command.progress', (task) => {
    if (task.origin?.page === page) apply((prev) => mergeTask(prev, task));
  });

  // command 是命令名，text 是对话里显示的那句（也是进度条标题的来源）
  const send = useCallback(async ({ command, args, text, action, conversationId }) => {
    setSending(true);
    try {
      const r = await api.sendCommand(pid, {
        ...(action ? { action } : { command }), args, text, origin: { page, label: PAGE_TITLES[page] || page }, conversation_id: conversationId,
      });
      apply((prev) => mergeTask(prev, r.task));
      toast(`已发到工作台对话：${text || command}`, 'msg');
      return r;
    } catch (e) {
      toast(errorText(e), 'alert');
      return null;
    } finally {
      setSending(false);
    }
  }, [api, pid, page, apply]);

  return { tasks, active: pageTask(tasks, page), send, sending };
}

// 页头 eyebrow：插件标识只留拼图图标，悬停显示插件名，点了看插件详情（plugin-detail?pack=<包名>，与 F1、F2 的入口同一个参数）
export function PluginEyebrow({ pack, label }) {
  return (
    <span className="mr-plg" data-pop={`由「${label}」插件提供`} aria-label={label} onClick={() => navigate(pagePath('plugin-detail'), { pack })}>
      <Icon name="puzzle" size={12} />
    </span>
  );
}

// 发命令的按钮：只往工作台对话发一条命令，页面上显示进度
export function SendButton({
  tasks, command, args, text, hint, children, disabled, ...rest
}) {
  return (
    <Button
      {...rest}
      disabled={disabled || tasks?.sending}
      data-pop={sendHint(hint || text || command)}
      onClick={() => tasks.send({ command, args, text: text || hint })}
    >
      {children}
    </Button>
  );
}

// 包声明的附加操作（PL-12 entries，kind=extra_action、where=这一页）：放在页头操作区，和页面自己的按钮一样只发命令
export function ExtraActions({ page, tasks }) {
  const { extensions } = useProject();
  const items = (extensions?.entries || []).filter((e) => e.kind === 'extra_action' && e.where === page && e.command);
  return items.map((e) => (
    <SendButton key={e.id} tasks={tasks} command={e.command} text={e.label} icon="sparkle" kind="soft">{e.label}</SendButton>
  ));
}

// 进度条：助手在工作台跑这一页发的命令（设计稿 mrRun）；没有进行中的任务时不显示
export function CommandRunBar({ task, detail, className, style }) {
  const { pid } = useProject();
  if (!isTaskActive(task)) return null;
  const pct = taskPercent(task);
  return (
    <div className={cx('mr-run', className)} style={style}>
      <span className="spin" style={{ width: 13, height: 13 }} />
      <div className="grow" style={{ minWidth: 0 }}>
        <b className="mr-run-t">{task.title}</b>
        <div className="xs mr-run-d">{taskDetail(task, detail)}</div>
      </div>
      <div className="mr-run-p"><i style={{ width: `${pct ?? 0}%` }} /></div>
      <Button size="xs" icon="msg" onClick={() => navigate(pagePath('wb-l2', pid), task.conversation_id ? { c: task.conversation_id } : undefined)}>去对话里看</Button>
    </div>
  );
}

/**
 * 写入范围只读条（设计稿 mr-lock）：targets 在锁的范围里才显示；不给 targets 时只要有锁就显示。
 * text 按两态给：{ running: [标题, 说明], pending: [标题, 说明] }；自主构建那一态用 running 的文字。
 */
export function ScopeLockBar({ lock, targets, text, className, style }) {
  const { pid } = useProject();
  const st = lockState(lock);
  if (st === 'none') return null;
  if (targets && !targets.some((t) => isLocked(lock, t))) return null;
  const [title, desc] = (st === 'pending_confirm' ? text?.pending : text?.running) || [];
  const conv = lock.conversation_id ? { c: lock.conversation_id } : undefined;
  let action;
  if (st === 'pending_confirm') action = <Button size="xs" kind="primary" icon="merge" onClick={() => navigate(pagePath('inbox', pid))}>去处理</Button>;
  else if (st === 'autobuild_slice') action = <Button size="xs" onClick={() => navigate(pagePath('l4-run', pid))}>查看运行</Button>;
  else action = <Button size="xs" onClick={() => navigate(pagePath('wb-l2', pid), conv)}>回到对话</Button>;
  return (
    <div className={cx('mr-lock', className)} style={style}>
      <Icon name="lock" size={14} />
      <div className="grow">
        <b>{title || (st === 'pending_confirm' ? '和助手这一轮的修改有冲突，选好保留哪边后才能编辑' : '助手正在改这部分内容，这一轮结束后才能编辑')}</b>
        {desc ? <div className="xs" style={{ marginTop: 3, color: '#92400E' }}>{desc}</div> : null}
      </div>
      {action}
    </div>
  );
}

// 插件页面在本项目是否启用（PL-12 的 nav / subpages）；扩展项还没读到时为 null
export function usePageEnabled(page) {
  const { extensions } = useProject();
  if (!extensions) return null;
  return (extensions.nav || []).some((n) => n.id === page) || (extensions.subpages || []).some((s) => s.id === page);
}

// 插件没启用时整页显示的空状态（直接打开地址、或刚在项目设置里停用）
export function PluginOff({ label, what }) {
  const { pid } = useProject();
  return (
    <div className="card" style={{ maxWidth: 640, margin: '40px auto' }}>
      <Empty
        icon="puzzle"
        title={`本项目没有启用「${label}」`}
        actions={(
          <>
            <Button kind="primary" icon="gear" onClick={() => navigate(pagePath('proj-settings', pid), { section: 'plugins' })}>去项目设置启用</Button>
            <Button icon="arrowl" onClick={() => navigate(pagePath('wb-l2', pid))}>回到工作台</Button>
          </>
        )}
      >
        {what ? `已有的${what}仍在知识包里` : null}
      </Empty>
    </div>
  );
}
