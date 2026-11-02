// 流程图弹窗的纯逻辑：打开时选中哪个节点、锁定条怎么说
import { lockState } from '@onto/state/readonly.js';

// 打开时先选中口径待定的节点：它是这张图里最需要人看的地方
export function initialSelection(model) {
  const n = (model?.nodes || []).find((x) => x.pending);
  return n ? { type: 'node', id: n.id } : null;
}

// 锁定条（设计稿 fcm-lock）：只说哪一片、到什么时候只读；右侧按钮给出下一步
export function lockBarText(lock, info) {
  const slice = lock?.slice?.name;
  switch (lockState(lock)) {
    case 'assistant_running':
      return { title: slice ? `助手正在改「${slice}」，确认生效或撤回前只读` : info?.text || '' };
    case 'pending_confirm':
      return { title: slice ? `「${slice}」的修改待确认，确认生效或撤回前只读` : info?.text || '' };
    case 'autobuild_slice':
      return { title: slice ? `自主构建正在做「${slice}」，这一片完成前只读` : info?.text || '' };
    default:
      return null;
  }
}
