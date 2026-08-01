import React, { useEffect, useState } from 'react';

/**
 * 阻塞性待办渲染在消息流末尾，用户往上翻看历史时它会滚出视野，
 * 界面上不再有任何「还有一件事等你处理」的痕迹——这正是「不知道怎么继续」的一种来源。
 * 这里在待办不可见时浮出一条提示，点击滚回去。
 */
export default function PendingInteractionHint({ scrollRef, targetSelector, label, onFocus }) {
  const [hidden, setHidden] = useState(true);

  useEffect(() => {
    const container = scrollRef?.current;
    if (!container) return undefined;
    const update = () => {
      const target = container.querySelector(targetSelector);
      if (!target) { setHidden(true); return; }
      const containerRect = container.getBoundingClientRect();
      const rect = target.getBoundingClientRect();
      const visible = rect.top < containerRect.bottom && rect.bottom > containerRect.top;
      setHidden(visible);
    };
    update();
    container.addEventListener('scroll', update, { passive: true });
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    observer?.observe(container);
    return () => {
      container.removeEventListener('scroll', update);
      observer?.disconnect();
    };
  }, [scrollRef, targetSelector, label]);

  if (hidden) return null;
  return (
    <button type="button" className="pchat-pending-hint" onClick={() => {
      const target = scrollRef?.current?.querySelector(targetSelector);
      target?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      onFocus?.();
    }}>
      <span className="pchat-pending-hint__dot" aria-hidden="true" />
      {label}
    </button>
  );
}
