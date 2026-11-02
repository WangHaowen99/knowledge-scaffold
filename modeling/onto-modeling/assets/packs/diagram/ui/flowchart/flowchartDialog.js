// 流程图弹窗的纯逻辑：打开时选中哪个节点、锁定条怎么说
import { lockState } from '@onto/state/readonly.js';

// 打开时先选中口径待定的节点：它是这张图里最需要人看的地方
export function initialSelection(model) {
  const n = (model?.nodes || []).find((x) => x.pending);
  return n ? { type: 'node', id: n.id } : null;
}

// 锁定条（设计稿 fcm-lock）：标题说清楚哪一片、为什么只读，说明行说片外照常编辑
export function lockBarText(lock, info) {
  const slice = lock?.slice?.name;
  switch (lockState(lock)) {
    case 'assistant_running':
      return {
        title: slice ? `助手正在改「${slice}」这一片，这张图在写入范围里，确认生效或撤回后才能编辑` : info?.text || '',
        desc: '片外的条目照常编辑；也可以回到对话里打断它',
      };
    case 'pending_confirm':
      return {
        title: slice ? `「${slice}」这一片的修改还没确认，这张图在写入范围里，确认生效或撤回后才能编辑` : info?.text || '',
        desc: '助手已经停下来等你确认；片外的条目照常编辑',
      };
    case 'autobuild_slice':
      return {
        title: slice ? `自主构建正在做「${slice}」这一片，这张图在写入范围里，这一片自动合入后才能编辑` : info?.text || '',
        desc: '片外的条目照常编辑',
      };
    default:
      return null;
  }
}
