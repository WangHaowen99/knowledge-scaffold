import { useLayoutEffect, useState } from 'react';

/** 元素顶部到滚动容器内容区底部的剩余高度（不小于 minHeight）。纯函数，便于测试。 */
export function remainingHeight({ containerBottom, containerPaddingBottom, elementTop, minHeight }) {
  // 减 1 像素抵消小数取整，避免外层容器因差 1px 出现滚动条
  return Math.max(minHeight, Math.floor(containerBottom - containerPaddingBottom - elementTop) - 1);
}

function scrollContainer(element) {
  for (let node = element.parentElement; node; node = node.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(node).overflowY)) return node;
  }
  return document.documentElement;
}

/**
 * 让元素向下填满所在滚动容器的剩余高度：返回从元素顶部到容器内容区底部的像素数。
 * 容器或窗口尺寸变化时重算；元素上方内容变化会移动元素位置，调用方通过 deps 触发重算。
 */
export function useRemainingHeight(ref, { minHeight = 240, deps = [] } = {}) {
  const [height, setHeight] = useState(minHeight);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    const container = scrollContainer(element);
    const update = () => {
      const containerRect = container.getBoundingClientRect();
      setHeight(remainingHeight({
        containerBottom: Math.min(containerRect.bottom, window.innerHeight),
        containerPaddingBottom: parseFloat(getComputedStyle(container).paddingBottom) || 0,
        elementTop: element.getBoundingClientRect().top,
        minHeight,
      }));
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(container);
    window.addEventListener('resize', update);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', update);
    };
  }, [ref, minHeight, ...deps]);

  return height;
}
