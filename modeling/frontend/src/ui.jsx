import { useEffect, useLayoutEffect, useRef, useState } from 'react';

// 耗时操作的经过时间（秒）：busy 为真时从 0 开始计
export function useElapsed(busy) {
  const [sec, setSec] = useState(0);
  const timer = useRef(null);
  useEffect(() => {
    if (busy) {
      setSec(0);
      timer.current = setInterval(() => setSec((s) => s + 1), 1000);
    }
    return () => clearInterval(timer.current);
  }, [busy]);
  return sec;
}

const fmt = (s) => (s >= 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s} 秒`);

// 随内容自动增高的多行输入：长问题/长答案完整换行展示，不再单行截断。
// 测高前后保存/还原滚动祖先的 scrollTop——height:auto 瞬时塌缩会让贴底的滚动容器被钳制跳动
function fitHeight(el, widthRef) {
  if (!el) return;
  widthRef.current = el.clientWidth;
  const saved = [];
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (p.scrollTop) saved.push([p, p.scrollTop]);
  }
  el.style.height = 'auto';
  el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
  for (const [p, top] of saved) p.scrollTop = top;
}

export function AutoTextarea({ value, ...rest }) {
  const ref = useRef(null);
  const widthRef = useRef(0);
  useLayoutEffect(() => { fitHeight(ref.current, widthRef); }, [value]);
  // 栏宽/窗口变化后重排出的新行会被 overflow:hidden 裁掉，需按宽度变化重测（只看宽度，避免自身写高度造成回环）
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      if (el.clientWidth !== widthRef.current) fitHeight(el, widthRef);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return <textarea ref={ref} rows={1} value={value} {...rest} />;
}

// 长任务按钮：busy 时显示动画点 + 已等待时长 + 预期提示
export function BusyButton({ busy, busyText, expect, children, className = 'btn btn-p', ...rest }) {
  const sec = useElapsed(busy);
  return (
    <button className={className} disabled={busy} {...rest}>
      {busy
        ? <span className="busy-inner"><span className="spinner" />{busyText || '处理中'}… 已等待 {fmt(sec)}{expect ? `（${expect}）` : ''}</span>
        : children}
    </button>
  );
}
