import { useCallback, useLayoutEffect, useRef, useState } from 'react';

import { isNearScrollBottom } from '../lib/scrollPolicy';

export default function useSmartScroll(contentVersion, scopeKey = '') {
  const containerRef = useRef(null);
  const followRef = useRef(true);
  const frameRef = useRef(null);
  const scopeFrameRef = useRef(null);
  const [isFollowing, setIsFollowing] = useState(true);

  const setFollowing = useCallback((next) => {
    followRef.current = next;
    setIsFollowing(next);
  }, []);

  const scrollToBottom = useCallback((behavior = 'smooth') => {
    const element = containerRef.current;
    if (!element) return;
    setFollowing(true);
    if (typeof element.scrollTo === 'function') {
      element.scrollTo({ top: element.scrollHeight, behavior });
    } else {
      element.scrollTop = element.scrollHeight;
    }
  }, [setFollowing]);

  const onScroll = useCallback((event) => {
    const element = event.currentTarget;
    setFollowing(isNearScrollBottom(element));
  }, [setFollowing]);

  useLayoutEffect(() => {
    followRef.current = true;
    const schedule = globalThis.requestAnimationFrame || ((callback) => setTimeout(callback, 0));
    const cancel = globalThis.cancelAnimationFrame || clearTimeout;
    scopeFrameRef.current = schedule(() => {
      setIsFollowing(true);
      const element = containerRef.current;
      if (element) element.scrollTop = element.scrollHeight;
      scopeFrameRef.current = null;
    });
    return () => {
      if (scopeFrameRef.current != null) cancel(scopeFrameRef.current);
      scopeFrameRef.current = null;
    };
  }, [scopeKey]);

  useLayoutEffect(() => {
    if (!followRef.current || !containerRef.current) return undefined;
    const schedule = globalThis.requestAnimationFrame || ((callback) => setTimeout(callback, 0));
    const cancel = globalThis.cancelAnimationFrame || clearTimeout;
    frameRef.current = schedule(() => {
      const element = containerRef.current;
      if (element && followRef.current) element.scrollTop = element.scrollHeight;
      frameRef.current = null;
    });
    return () => {
      if (frameRef.current != null) cancel(frameRef.current);
      frameRef.current = null;
    };
  }, [contentVersion]);

  return {
    containerRef,
    isFollowing,
    onScroll,
    scrollToBottom,
  };
}
