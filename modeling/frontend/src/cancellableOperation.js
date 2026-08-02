/**
 * 忙碌态到「可取消的后台操作」的映射。
 *
 * 只有真正跑在后台、且在检查点会读取消标记的长任务才给取消按钮。给一个点了不起作用
 * 的取消按钮，比不给更糟——用户会以为已经停了。
 *
 * 取消是「当前这一批跑完就停」，不是立即中断：单次模型调用发出去就收不回来。
 * 文案必须如实说明，见 CANCEL_PENDING_HINT。
 */
export const CANCELLABLE_BUSY_STATES = {
  analyze: 'analyze',
  generate_knowledge: 'knowledgeGenerate',
  repair_knowledge: 'knowledgeRepair',
  script_generation: 'scriptGeneration',
};

/** 服务端 run 的 intent → 可取消操作。刷新页面后本地 busy 没了，只能靠它认 */
const INTENT_TO_OPERATION = [
  [/资料分析|分析建模资料/, 'analyze'],
  [/知识结构|生成知识包/, 'knowledgeGenerate'],
  [/修复|修缮/, 'knowledgeRepair'],
  [/脚本/, 'scriptGeneration'],
];

export const CANCEL_PENDING_HINT = '正在停止：当前这一批处理完就会停下，已完成的部分会保留。';

export function cancellableOperationOf({ busy, serverRun } = {}) {
  const direct = CANCELLABLE_BUSY_STATES[busy];
  if (direct) return direct;
  if (busy !== 'server_run') return null;
  const intent = String(serverRun?.intent ?? '');
  return INTENT_TO_OPERATION.find(([pattern]) => pattern.test(intent))?.[1] ?? null;
}
