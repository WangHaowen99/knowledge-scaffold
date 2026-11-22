import { createElement, useEffect, useMemo, useRef, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

type Range = { from: number; to: number };
type Positioned = { node?: { position?: { start: { line: number }; end: { line: number } } }; children?: ReactNode };

const BLOCKS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr', 'pre', 'blockquote'] as const;

const image: Components['img'] = ({ alt }) => <span className="img-ph">[图片{alt ? `：${alt}` : ''}]</span>;

/** Markdown; `highlight` marks the blocks on those source lines (1-based, of `text`) and scrolls to the first. */
export default function Markdown({ text, highlight }: { text: string; highlight?: Range | null }) {
  const root = useRef<HTMLDivElement>(null);
  const components = useMemo<Components>(() => {
    if (!highlight) return { img: image };
    const marked: Record<string, unknown> = { img: image };
    for (const tag of BLOCKS) {
      marked[tag] = ({ node, children, ...rest }: Positioned & Record<string, unknown>) => {
        const pos = node?.position;
        const hit = pos ? pos.start.line <= highlight.to && pos.end.line >= highlight.from : false;
        return createElement(tag, { ...rest, className: hit ? 'md-hit' : undefined }, children);
      };
    }
    return marked as Components;
  }, [highlight?.from, highlight?.to]);

  useEffect(() => {
    if (highlight) root.current?.querySelector('.md-hit')?.scrollIntoView({ block: 'center' });
  }, [highlight?.from, highlight?.to, text]);

  return (
    <div className="markdown" ref={root}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
