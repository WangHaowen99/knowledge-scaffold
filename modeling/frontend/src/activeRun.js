/**
 * 「后台还在跑什么」以服务端运行记录为准。
 *
 * 本地 busy 只覆盖提交那一瞬：刷新页面、换标签页、换设备之后它就没了，而后端还在跑。
 * 用户看到转圈消失会以为提交丢了，实际上正在处理——这类误导比不显示更糟。
 *
 * package_chat 早就是这么做的（见 packageChatRun.js），这里把同样的做法推广到
 * 所有活动运行：待办提交、里程碑确认、脚本生成都会留下 run 记录。
 */
const RUNNING_STATUSES = new Set(['queued', 'running']);

/** 当前最该展示的活动运行：取最早开始的那个，用户最关心已经等了多久 */
export function activeRunOf(authoring) {
  const runs = (authoring?.activeRuns ?? []).filter((run) => RUNNING_STATUSES.has(run?.status));
  if (!runs.length) return null;
  return runs.reduce((oldest, run) => {
    const left = Date.parse(run?.createdAt ?? '');
    const right = Date.parse(oldest?.createdAt ?? '');
    if (!Number.isFinite(left)) return oldest;
    if (!Number.isFinite(right)) return run;
    return left < right ? run : oldest;
  });
}

/** 活动运行的展示文案。intent 是后端写的人话意图，如「确认建模目标」 */
export function activeRunLabel(run) {
  const intent = String(run?.intent ?? '').trim();
  return intent ? `正在${intent}` : '正在处理';
}

/**
 * 后端是否正持有这个项目的写租约（生成脚本方案、调整建模方案等长耗时操作）。
 *
 * 这类操作全程在请求里跑，期间待办在库里仍是 pending。刷新页面待办就会原样回来，
 * 看着像是没提交过——再点一次必然撞上租约，回一条「项目正在处理中」。
 * 拿到这个信号，界面就能把待办显示成「已提交，正在处理」而不是一个点了就报错的表单。
 */
export function activeOperationOf(session) {
  const operation = String(session?.activeOperation?.operation ?? '').trim();
  if (!operation) return null;
  return { operation, since: session.activeOperation.since ?? '' };
}
