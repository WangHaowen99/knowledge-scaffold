/* 模拟 agent 活动流：驱动对话流底部的活动行。
   接后端后换成 SSE，组件不需要改——它们只读 store.activity。 */
import { actions } from '../appStore.js';
import { activityFrames } from './fixtures.js';

export function startMockActivity() {
  let i = 0;
  let timer = null;
  const startedAt = Date.now();

  const tick = () => {
    const f = activityFrames[i % activityFrames.length];
    actions.setActivity({ ...f, startedAt });
    // 助手干到哪，中栏就跟到哪——follow=false 时 agentFocus 会自动改为攒起来
    if (f.focus) actions.agentFocus(f.focus);
    i += 1;
    timer = setTimeout(tick, 6000);
  };
  timer = setTimeout(tick, 400);
  return () => { clearTimeout(timer); actions.setActivity(null); };
}

/** 把毫秒差转成「1分12秒」。长任务里这个数字比转圈有用得多。 */
export function elapsedText(startedAt, now) {
  const sec = Math.max(0, Math.floor((now - startedAt) / 1000));
  if (sec < 60) return `已 ${sec} 秒`;
  return `已 ${Math.floor(sec / 60)}分${String(sec % 60).padStart(2, '0')}秒`;
}
