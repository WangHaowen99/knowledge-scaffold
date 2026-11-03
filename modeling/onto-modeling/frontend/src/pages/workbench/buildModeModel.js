// 对话里切换构建方式（F2 第 8 条）：陪伴建模 L2 / 自主构建 L4。纯函数：切到哪种要做什么
import { ENDED_STATES } from './autobuildModel.js';

export const MODE_LABEL = { L2: '陪伴建模', L4: '自主构建' };
// 运行能转为陪伴（AB-08）的状态；stopping 在去安全点停下，等它停
const CONVERTIBLE = ['running', 'queued', 'pausing', 'paused', 'interrupted'];

/**
 * @param {{target: 'L2'|'L4', mode: 'L2'|'L4', runState: string|null, blueprintState: 'draft'|'confirmed'|null}} o
 *   runState 是这个项目没结束的自主构建运行的状态（没有为 null）
 * @returns {'noop'|'busy'|'convert'|'set'|'set_wait_blueprint'|'set_then_confirm'|'set_then_build'}
 *   noop 不用做；busy 运行正在停下；convert 运行中转为陪伴（AB-08）并把默认改成陪伴；set 只改默认（P-13）；
 *   set_wait_blueprint 改默认，还没有蓝图（确认蓝图时按自主构建开始）；set_then_confirm 改默认后先引导确认蓝图；
 *   set_then_build 改默认后走开始前预览、开始（AB-03、AB-04）
 */
export function planModeSwitch({ target, mode, runState, blueprintState }) {
  if (runState && !ENDED_STATES.includes(runState)) {
    if (target === 'L4' || runState === 'converting') return 'noop';
    return CONVERTIBLE.includes(runState) ? 'convert' : 'busy';
  }
  if (target === 'L2') return mode === 'L2' ? 'noop' : 'set';
  if (blueprintState === 'confirmed') return 'set_then_build';
  if (blueprintState === 'draft') return 'set_then_confirm';
  return 'set_wait_blueprint';
}

// 输入框旁的模式胶囊：运行没结束时说运行的状态，否则说项目的构建方式
export function modeChipLabel({ mode, runState }) {
  if (runState && !ENDED_STATES.includes(runState)) {
    if (runState === 'converting') return '转为陪伴中';
    return ['paused', 'interrupted'].includes(runState) ? '自主构建 · 已暂停' : '自主构建中';
  }
  return MODE_LABEL[mode] || MODE_LABEL.L2;
}
