import { useEffect, useRef, useState } from 'react';
import type { PDFDocumentProxy } from 'pdfjs-dist/legacy/build/pdf.mjs';
import { PdfPages, openPdf } from './PdfView';
import { loadBlob, type LoadedFile } from './fileStore';
import { formatBytes } from './format';
import { MAX_PREVIEW_BYTES, previewKind, type Focus, type PreviewKind } from './preview';
import { errorText } from './toast';
import type { DocumentItem } from './types';
import { Empty, Spinner } from './ui';

type Load = { read: number; total: number | null };

function Loading({ load }: { load: Load | null }) {
  const ratio = load?.total ? Math.min(1, load.read / load.total) : null;
  return (
    <div className="empty">
      <Spinner size={18} />
      {load && load.read > 0 ? (
        <div className="file-progress">
          {ratio !== null ? (
            <div className="progress">
              <div style={{ width: `${Math.round(ratio * 100)}%` }} />
            </div>
          ) : null}
          <span className="muted num">
            {formatBytes(load.read)}
            {load.total ? ` / ${formatBytes(load.total)}` : ''}
          </span>
        </div>
      ) : null}
    </div>
  );
}

function Pdf({ docId, ranged, focus }: { docId: string; ranged: boolean; focus?: Focus }) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [load, setLoad] = useState<Load | null>(null);
  useEffect(() => {
    let alive = true;
    openPdf(docId, ranged, (read, total) => alive && setLoad({ read, total }), (err) => alive && setError(errorText(err)))
      .then((doc) => alive && setPdf(doc))
      .catch((err) => alive && setError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [docId, ranged]);
  if (error) return <div className="error-box">{error}</div>;
  if (!pdf) return <Loading load={load} />;
  return <PdfPages pdf={pdf} focus={focus} />;
}

function Docx({ blob }: { blob: Blob }) {
  const body = useRef<HTMLDivElement>(null);
  const styles = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const el = body.current;
    if (!el || !styles.current) return undefined;
    let alive = true;
    el.replaceChildren();
    import('docx-preview')
      .then(({ renderAsync }) =>
        renderAsync(blob, el, styles.current ?? undefined, {
          className: 'docx-page',
          inWrapper: true,
          breakPages: true,
          ignoreLastRenderedPageBreak: true,
          renderHeaders: true,
          renderFooters: true,
          useBase64URL: true,
        }),
      )
      .then(() => {
        if (!alive) return;
        // pages have a fixed paper width: shrink them to the drawer instead of scrolling sideways
        const wrapper = el.querySelector<HTMLElement>('.docx-page-wrapper');
        const page = wrapper?.querySelector<HTMLElement>('section.docx-page');
        if (wrapper && page) {
          const fit = Math.min(1, el.clientWidth / (page.offsetWidth + 32));
          wrapper.style.zoom = String(fit);
        }
      })
      .catch((err) => alive && setError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [blob]);
  return (
    <>
      {error ? <div className="error-box">{error}</div> : null}
      <div ref={styles} />
      <div ref={body} className="docx-body" />
    </>
  );
}

function Media({ kind, url, time }: { kind: 'audio' | 'video'; url: string; time?: number }) {
  const el = useRef<HTMLMediaElement | null>(null);
  useEffect(() => {
    const media = el.current;
    if (!media || time === undefined) return;
    const seek = () => {
      media.currentTime = time;
    };
    if (media.readyState >= 1) seek();
    else media.addEventListener('loadedmetadata', seek, { once: true });
  }, [time, url]);
  return kind === 'video' ? (
    <video ref={(v) => void (el.current = v)} className="file-media" src={url} controls preload="metadata" />
  ) : (
    <audio ref={(a) => void (el.current = a)} className="file-media" src={url} controls preload="metadata" />
  );
}

function Blobbed({ docId, doc, kind, focus }: { docId: string; doc: DocumentItem; kind: Exclude<PreviewKind, 'pdf' | 'none'>; focus?: Focus }) {
  const [file, setFile] = useState<LoadedFile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [load, setLoad] = useState<Load | null>(null);
  useEffect(() => {
    let alive = true;
    loadBlob(docId, doc.file_type, (read, total) => alive && setLoad({ read, total }))
      .then((f) => alive && setFile(f))
      .catch((err) => alive && setError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [docId, doc.file_type]);
  if (error) return <div className="error-box">{error}</div>;
  if (!file) return <Loading load={load} />;
  if (kind === 'image') return <img className="file-image" src={file.url} alt={doc.title} />;
  if (kind === 'docx') return <Docx blob={file.blob} />;
  return <Media kind={kind} url={file.url} time={focus?.time} />;
}

/** Whether the drawer offers the original file as a view: a file of a kind the page can show. */
export function canPreview(doc: DocumentItem): boolean {
  return doc.source_type === 'file' && previewKind(doc.file_type) !== 'none';
}

/** The original file of a document, shown as itself. */
export default function FileView({ doc, ranged, focus }: { doc: DocumentItem; ranged: boolean; focus?: Focus }) {
  const kind = previewKind(doc.file_type);
  if ((doc.file_size ?? 0) > MAX_PREVIEW_BYTES) return <Empty title="文件过大，无法预览" />;
  if (kind === 'none') return <Empty title="无法预览这种文件" />;
  if (kind === 'pdf') return <Pdf docId={doc.id} ranged={ranged} focus={focus} />;
  return <Blobbed docId={doc.id} doc={doc} kind={kind} focus={focus} />;
}
