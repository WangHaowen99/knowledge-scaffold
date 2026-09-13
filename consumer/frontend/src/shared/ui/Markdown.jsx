import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const INITIAL_TABLE_SCROLL_STATE = {
  isOverflowing: false,
  canScrollLeft: false,
  canScrollRight: false,
};

function ScrollableTable({ children }) {
  const scrollRef = useRef(null);
  const [scrollState, setScrollState] = useState(INITIAL_TABLE_SCROLL_STATE);

  const updateScrollState = useCallback(() => {
    const scrollRegion = scrollRef.current;
    if (!scrollRegion) {
      return;
    }

    const maxScrollLeft = Math.max(
      0,
      scrollRegion.scrollWidth - scrollRegion.clientWidth,
    );
    const scrollLeft = Math.max(0, scrollRegion.scrollLeft);
    const nextState = {
      isOverflowing: maxScrollLeft > 1,
      canScrollLeft: maxScrollLeft > 1 && scrollLeft > 1,
      canScrollRight: maxScrollLeft > 1 && scrollLeft < maxScrollLeft - 1,
    };
    setScrollState((currentState) =>
      currentState.isOverflowing === nextState.isOverflowing &&
      currentState.canScrollLeft === nextState.canScrollLeft &&
      currentState.canScrollRight === nextState.canScrollRight ?
        currentState :
        nextState,
    );
  }, []);

  useEffect(() => {
    const scrollRegion = scrollRef.current;
    if (!scrollRegion) {
      return undefined;
    }

    updateScrollState();
    const resizeObserver =
      typeof window.ResizeObserver === 'function' ?
        new window.ResizeObserver(updateScrollState) :
        null;
    resizeObserver?.observe(scrollRegion);
    const table = scrollRegion.querySelector('table');
    if (table) {
      resizeObserver?.observe(table);
    }
    window.addEventListener('resize', updateScrollState);

    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener('resize', updateScrollState);
    };
  }, [updateScrollState]);

  return (
    <div
      className="mdv-table-shell"
      data-scroll-left={scrollState.canScrollLeft ? 'true' : undefined}
      data-scroll-right={scrollState.canScrollRight ? 'true' : undefined}
    >
      {scrollState.isOverflowing && (
        <div className="mdv-table-scroll-hint" aria-hidden="true">
          ↔ 左右滚动查看更多列
        </div>
      )}
      <div
        ref={scrollRef}
        className="mdv-table-scroll"
        role={scrollState.isOverflowing ? 'region' : undefined}
        aria-label={scrollState.isOverflowing ? '可横向滚动的表格' : undefined}
        tabIndex={scrollState.isOverflowing ? 0 : undefined}
        onScroll={updateScrollState}
      >
        <table>{children}</table>
      </div>
    </div>
  );
}

// 统一的 Markdown 视图：知识包文档与对话气泡共用，样式见 shared/ui/ui.css。
export default function Markdown({ children }) {
  return (
    <div className="mdv">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ScrollableTable,
          pre({ children: preChildren, ...props }) {
            const code = Array.isArray(preChildren) ?
              preChildren[0] :
              preChildren;
            const lang =
              /language-(?<language>\w+)/.exec(code?.props?.className || '')
                ?.groups.language || '';
            return (
              <pre data-lang={lang} {...props}>
                {preChildren}
              </pre>
            );
          },
          a({ ...props }) {
            return <a target="_blank" rel="noreferrer" {...props} />;
          },
        }}
      >
        {typeof children === 'string' ? children : ''}
      </ReactMarkdown>
    </div>
  );
}
