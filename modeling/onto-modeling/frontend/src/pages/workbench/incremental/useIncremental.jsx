// 工作台里的增量更新（设计稿 incremental，内核能力，不单设页面）：
// - 进入 incremental 时落到哪条对话：地址带 ?c= 的 > 待处理里进行中的变更清单所在的对话（IN-01 → INC-01 的 conversation_id）
//   > 已执行未确认的清单所在的对话（待确认的助手变更集，CS-01）> 当前对话。
//   建议条照服务端给的显示：上传了新资料要增量更新时由推荐第 2 级推「增量更新」（节点模型重构 2b），这里不另起一份。
// - 变更点清单弹窗（?dialog=change_list 或卡片双击）、「确认清单并执行」（INC-03 补接受 + INC-04）。
import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { navigate, pagePath } from '../../../app/router.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import * as M from './model.js';

const ChangeListDialog = lazy(() => import('./ChangeListDialog.jsx'));

// 进入 incremental 时要去的对话；ready 之前工作台先不画时间线，免得闪一下别的对话
export function useIncrementalTarget(route) {
  const { api } = useApp();
  const { pid } = useProject();
  const on = route.page === 'incremental' && !route.query?.c;
  const [target, setTarget] = useState(() => ({ ready: !on, cid: null, changeListId: null }));
  useEffect(() => {
    if (!on) { setTarget({ ready: true, cid: null, changeListId: null }); return undefined; }
    let alive = true;
    setTarget({ ready: false, cid: null, changeListId: null });
    (async () => {
      const out = { ready: true, cid: null, changeListId: null };
      try {
        const id = M.changeListIdFromInbox((await api.inbox(pid, { filter: 'change_list', limit: 5 })).items);
        if (id) {
          out.changeListId = id;
          const cl = await api.changeList(pid, id);
          const convs = cl.conversation_id ? await api.conversations(pid) : null;
          if (convs?.items?.some((c) => c.id === cl.conversation_id)) out.cid = cl.conversation_id;
        } else {
          const [cs, convs] = await Promise.all([api.changesets(pid, { state: 'pending_confirm' }), api.conversations(pid)]);
          out.cid = M.pendingChangesetConversation(cs.items, convs.items);
        }
      } catch { /* 找不到就留在当前对话 */ }
      if (alive) setTarget(out);
    })();
    return () => { alive = false; };
  }, [api, pid, on]);
  return target;
}

export function useIncremental({ route, target, conv, state, mainRef }) {
  const { api } = useApp();
  const { pid } = useProject();
  const on = route.page === 'incremental';
  const [dialog, setDialog] = useState(null);
  const arrived = useRef(null);
  const scrolled = useRef(null);
  const stale = useRef(null);
  // 到了目标对话：刚切过去时时间线还是旧对话的内容（同一个 state 对象），等它换成新对话的才算到了；本来就在这条对话上的直接算到了
  if (target.cid && conv.cid !== target.cid) stale.current = state;
  if (target.cid && conv.cid === target.cid && state !== stale.current) arrived.current = target.cid;
  useEffect(() => { if (!on) scrolled.current = null; }, [on]);

  // 地址带 ?dialog=change_list（建议条「逐条修改清单」、别的页面跳过来）
  const want = route.query?.dialog === 'change_list' ? (route.query.change_list_id || M.latestChangeListCard(state)?.change_list_id || target.changeListId) : null;
  useEffect(() => { if (want) setDialog({ id: want }); }, [want]);
  const closeDialog = useCallback(() => {
    setDialog(null);
    if (route.query?.dialog === 'change_list') navigate(pagePath(route.page, pid), route.query.c ? { c: route.query.c } : undefined);
  }, [route.page, route.query?.dialog, route.query?.c, pid]);
  const openChangeList = useCallback((id) => setDialog({ id }), []);

  // 确认清单并执行：低把握的没单独确认就打开弹窗；其余没确认的一并接受，再执行
  const execute = useCallback(async (id) => {
    try {
      const cl = await api.changeList(pid, id);
      if (cl.state === 'executed') { toast('这份清单已经执行过了', 'info'); return; }
      const plan = M.executePlan(cl.points);
      if (plan.blocked.length) {
        setDialog({ id });
        toast(`第 ${plan.blocked.map((p) => p.n).join('、')} 条需先单独确认`, 'alert');
        return;
      }
      let revision = cl.revision;
      if (plan.accept.length) revision = (await api.acceptChangePoints(pid, id, { point_ids: plan.accept })).revision;
      await api.executeChangeList(pid, id, { base_revision: revision });
      const m = mainRef.current;
      if (m) m.scrollTop = m.scrollHeight;
    } catch (e) {
      if (e.code === 'invalid_argument' && e.details?.unconfirmed?.length) setDialog({ id });
      toast(errorText(e), 'alert');
    }
  }, [api, pid, mainRef]);

  const onAction = useCallback(async (a) => {
    if (a.type === 'execute_change_list') { await execute(a.change_list_id); return true; }
    return false;
  }, [execute]);

  // 落到清单所在的对话之前不画时间线；用户自己切走了不再拦
  const hideTimeline = on && (!target.ready || (!!target.cid && arrived.current !== target.cid));

  // 打开时停在等确认的清单卡上（设计稿默认态：卡片顶端离对话区顶部 64px）；只在落到这条对话时做一次
  const scrollKey = on && !hideTimeline && state.lastSeq && scrolled.current !== conv.cid ? conv.cid : null;
  useEffect(() => {
    if (!scrollKey) return;
    scrolled.current = scrollKey;
    const card = M.latestChangeListCard(state);
    if (!card || card.state === 'executed') return;
    const cardId = `inc-cl-${card.change_list_id}`;
    setTimeout(() => {
      const m = mainRef.current;
      const el = document.getElementById(cardId);
      if (!m || !el) return;
      m.style.scrollBehavior = 'auto';
      m.scrollTop = el.getBoundingClientRect().top - m.getBoundingClientRect().top + m.scrollTop - 64;
      m.style.scrollBehavior = '';
    }, 30);
  }, [scrollKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const element = dialog ? <Suspense fallback={null}><ChangeListDialog key={dialog.id} id={dialog.id} onClose={closeDialog} /></Suspense> : null;
  return { hideTimeline, onAction, openChangeList, execute, element };
}
