import { useEffect, useRef, useState } from 'react';

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
