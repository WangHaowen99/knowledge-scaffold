import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { PDFDocumentProxy } from 'pdfjs-dist/legacy/build/pdf.mjs';
import { hold, readRange, readWhole, tooLarge, type Progress } from './fileStore';
import { MAX_PREVIEW_BYTES, bboxStyle, type Focus } from './preview';

type PdfJs = typeof import('pdfjs-dist/legacy/build/pdf.mjs');

let library: Promise<PdfJs> | null = null;

/** pdf.js (legacy build: polyfilled for older browsers), loaded only when a PDF is opened. */
function pdfjs(): Promise<PdfJs> {
  if (!library) {
    library = Promise.all([import('pdfjs-dist/legacy/build/pdf.mjs'), import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url')]).then(
      ([lib, worker]) => {
        lib.GlobalWorkerOptions.workerSrc = worker.default;
        return lib;
      },
    );
  }
  return library;
}

const FIRST_READ = 1024 * 1024;
const RANGE_CHUNK = 1024 * 1024;

/**
 * The document, held in the single file slot. A backend that reads ranges is read lazily (pdf.js asks
 * for the parts it needs); otherwise the whole file is read first.
 */
export function openPdf(docId: string, ranged: boolean, onProgress?: Progress, onError?: (err: unknown) => void): Promise<PDFDocumentProxy> {
  return hold(
    `${docId}|pdf`,
    async (stop, progress) => {
      const lib = await pdfjs();
      const common = { isEvalSupported: false };
      if (!ranged) {
        return lib.getDocument({ ...common, data: await readWhole(docId, stop, progress) }).promise;
      }
      const first = await readRange(docId, 0, FIRST_READ);
      const total = first.total;
      if (total !== null && total > MAX_PREVIEW_BYTES) throw tooLarge();
      if (total === null || total <= first.bytes.length) {
        return lib.getDocument({ ...common, data: total === null ? await readWhole(docId, stop, progress) : first.bytes }).promise;
      }
      const transport = new lib.PDFDataRangeTransport(total, first.bytes);
      transport.requestDataRange = (begin: number, end: number) => {
        if (stop.cancelled) return;
        readRange(docId, begin, end)
          .then((part) => !stop.cancelled && transport.onDataRange(begin, part.bytes))
          .catch((err) => onError?.(err));
      };
      return lib.getDocument({ ...common, range: transport, length: total, rangeChunkSize: RANGE_CHUNK, disableAutoFetch: true, disableStream: true })
        .promise;
    },
    (pdf) => void pdf.loadingTask.destroy(),
    onProgress,
  );
}

type Size = { width: number; height: number };

function Page({ pdf, n, scale, size, active, box }: { pdf: PDFDocumentProxy; n: number; scale: number; size: Size; active: boolean; box: number[] | null }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [own, setOwn] = useState<Size | null>(null);
  useEffect(() => {
    const el = canvas.current;
    if (!active || !el) return undefined;
    let task: { cancel: () => void } | null = null;
    let alive = true;
    void pdf.getPage(n).then((page) => {
      if (!alive) return;
      const ratio = window.devicePixelRatio || 1;
      const viewport = page.getViewport({ scale: scale * ratio });
      setOwn({ width: viewport.width / ratio, height: viewport.height / ratio });
      el.width = Math.floor(viewport.width);
      el.height = Math.floor(viewport.height);
      const render = page.render({ canvas: el, canvasContext: el.getContext('2d')!, viewport });
      task = render;
      render.promise.catch(() => undefined);
    });
    return () => {
      alive = false;
      task?.cancel();
      // a page far from view keeps no pixels
      el.width = 0;
      el.height = 0;
    };
  }, [pdf, n, scale, active]);
  const shown = own ?? size;
  const hit = box ? bboxStyle(box, scale) : null;
  return (
    <div className="pdf-page" data-page={n} style={{ width: shown.width, height: shown.height }}>
      <canvas ref={canvas} style={{ width: shown.width, height: shown.height }} />
      {hit ? <div className="pdf-hit" style={hit} /> : null}
    </div>
  );
}

/** Pages stacked at the drawer's width; only pages near the view are drawn. */
export function PdfPages({ pdf, focus }: { pdf: PDFDocumentProxy; focus?: Focus }) {
  const root = useRef<HTMLDivElement>(null);
  const [base, setBase] = useState<Size | null>(null);
  const [width, setWidth] = useState(0);
  const [near, setNear] = useState<Set<number>>(() => new Set([1]));
  const [current, setCurrent] = useState(1);
  const [typed, setTyped] = useState<string | null>(null);
  const count = pdf.numPages;
  const scale = base && width ? width / base.width : 1;

  useEffect(() => {
    void pdf.getPage(1).then((page) => {
      const v = page.getViewport({ scale: 1 });
      setBase({ width: v.width, height: v.height });
    });
  }, [pdf]);

  useEffect(() => {
    const el = root.current;
    if (!el) return undefined;
    const measure = () => setWidth(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const scroller = () => root.current?.closest('.drawer-body') as HTMLElement | null;

  // pages within a screen of the view are drawn, the rest released
  useEffect(() => {
    const el = root.current;
    if (!el || !base) return undefined;
    const io = new IntersectionObserver(
      (entries) =>
        setNear((cur) => {
          const next = new Set(cur);
          for (const e of entries) {
            const n = Number((e.target as HTMLElement).dataset.page);
            if (e.isIntersecting) next.add(n);
            else next.delete(n);
          }
          return next;
        }),
      { root: scroller(), rootMargin: '100% 0px' },
    );
    el.querySelectorAll('.pdf-page').forEach((p) => io.observe(p));
    return () => io.disconnect();
  }, [base, count, scale]);

  useEffect(() => {
    const sc = scroller();
    if (!sc) return undefined;
    const onScroll = () => {
      const top = sc.getBoundingClientRect().top + 60;
      const pages = root.current?.querySelectorAll<HTMLElement>('.pdf-page') ?? [];
      for (const p of pages) {
        if (p.getBoundingClientRect().bottom > top) {
          setCurrent(Number(p.dataset.page));
          break;
        }
      }
    };
    sc.addEventListener('scroll', onScroll, { passive: true });
    return () => sc.removeEventListener('scroll', onScroll);
  }, [base]);

  const go = (n: number, offset = 0) => {
    const page = root.current?.querySelector<HTMLElement>(`.pdf-page[data-page="${Math.min(count, Math.max(1, n))}"]`);
    const sc = scroller();
    if (!page || !sc) return;
    sc.scrollTop += page.getBoundingClientRect().top - sc.getBoundingClientRect().top - 48 + offset;
  };

  // evidence: its page, with the box in view
  useEffect(() => {
    if (!base || !width || !focus?.page) return;
    const hit = bboxStyle(focus.bbox, scale);
    window.requestAnimationFrame(() => go(focus.page as number, hit ? Math.max(0, hit.top - 80) : 0));
    // scale settles once the width is known
  }, [base, width, focus?.page, focus?.bbox?.join(',')]);

  const size = base ? { width: base.width * scale, height: base.height * scale } : { width, height: width * 1.3 };
  return (
    <div className="pdf" ref={root}>
      <div className="pdf-bar">
        <button className="icon-btn" disabled={current <= 1} onClick={() => go(current - 1)} aria-label="上一页">
          <ChevronLeft size={15} />
        </button>
        <input
          className="input pdf-page-input num"
          value={typed ?? String(current)}
          onFocus={(e) => e.target.select()}
          onChange={(e) => setTyped(e.target.value.replace(/\D/g, ''))}
          onBlur={() => setTyped(null)}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || !typed) return;
            go(Number(typed));
            setTyped(null);
          }}
          aria-label="页码"
        />
        <span className="muted num">/ {count}</span>
        <button className="icon-btn" disabled={current >= count} onClick={() => go(current + 1)} aria-label="下一页">
          <ChevronRight size={15} />
        </button>
      </div>
      {base && width
        ? Array.from({ length: count }, (_, i) => i + 1).map((n) => (
            <Page key={n} pdf={pdf} n={n} scale={scale} size={size} active={near.has(n)} box={focus?.page === n ? focus.bbox ?? null : null} />
          ))
        : null}
    </div>
  );
}
