// 对话区是否跟着新内容滚到底：离底部不到 STICK_GAP 算在底部。
// 点「回到底部」后平滑滚动的途中还没到底，但只往下走，仍算跟着；往上滚（用户打断）或到底以后按位置判断
export const STICK_GAP = 120;

export function scrollFollow(prev, { top, gap }) {
  const near = gap < STICK_GAP;
  const jumping = !!prev?.jumping && !near && top >= prev.top;
  return { jumping, top, stick: near || jumping };
}
