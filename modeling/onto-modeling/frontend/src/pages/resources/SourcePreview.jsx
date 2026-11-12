// 原始资料只读预览（develop SourcePreview 的元素与行为）：资料芯片、证据「查看原文」带锚点跳来时用。
// 文本按行、Word 按段与表格单元格分段显示并定位高亮（R-17）；PDF、图片在平台内打开（F-04 原样输出，不解析）；
// 其他格式给下载入口。Excel 走表格预览（SheetPane），不在这里。
import { useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Empty, Note, cx } from '../../ui/index.jsx';
import { anchorLabel, groupSegments } from '../../lib/sourceLocation.js';

const enc = encodeURIComponent;
const rawPath = (pid, path, download) => `/projects/${enc(pid)}/files/raw?path=${enc(path)}${download ? '&download=true' : ''}`;

// 原始资料只读：resource/ 下的资料不提供编辑入口
export function DownloadButton({ path, size = 'sm' }) {
  const { services } = useApp();
  const { pid } = useProject();
  const download = async () => {
    try {
      await services.download(rawPath(pid, path, true), path.split('/').pop());
      toast('已开始下载原文件', 'download');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return <Button size={size} icon="download" onClick={download}>下载原文件</Button>;
}

function PreviewHeader({ path, extra }) {
  return (
    <div className="row mb8 src-head">
      <span className="b ell grow" title={path}>{path}</span>
      {extra}
      <DownloadButton path={path} />
    </div>
  );
}

// 行号/段号列只显示编号本身；表格单元格在表内高亮，不进编号列
const segmentNumber = (anchor) => anchor.replace(/^(L|p:)/, '');

function SegmentTable({ block, target }) {
  const cells = new Map(block.cells.map((c) => [`${c.row}:${c.cell}`, c.segment]));
  return (
    <div className="src-table-wrap">
      <div className="src-table-title">表 {block.table}</div>
      <table className="src-table">
        <tbody>
          {Array.from({ length: block.rows }, (_, r) => (
            <tr key={r}>
              {Array.from({ length: block.cols }, (_, c) => {
                const segment = cells.get(`${r + 1}:${c + 1}`);
                const anchor = `table:${block.table}/row:${r + 1}/cell:${c + 1}`;
                return (
                  <td key={c} data-anchor={anchor} className={cx(anchor === target && 'src-hit')}>
                    {segment?.text ?? ''}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SegmentPreview({ path, anchor, nonce }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [data, setData] = useState(null);
  const bodyRef = useRef(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    api.resourceSourcePreview(pid, path, anchor)
      .then((r) => alive && setData(r))
      .catch((e) => alive && setData({ error: errorText(e), missing: e.code === 'file_not_found' }));
    return () => { alive = false; };
  }, [api, pid, path, anchor]);

  const blocks = useMemo(() => groupSegments(data?.segments), [data]);
  const hit = data?.anchor_found ? anchor : '';

  // 再次点击同一位置时 nonce 变化，重新滚动到定位点
  useEffect(() => {
    if (!hit || !bodyRef.current) return;
    const element = [...bodyRef.current.querySelectorAll('[data-anchor]')].find((node) => node.dataset.anchor === hit);
    element?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [hit, data, nonce]);

  if (!data) return <div className="evd-paper rs-loading"><span className="spin" />正在读取原文…</div>;
  if (data.error) {
    return (
      <>
        <Note tone="amber" icon="alert" className="mb8">{data.error}</Note>
        {data.missing ? null : <DownloadButton path={path} />}
      </>
    );
  }
  if (data.status !== 'ok') {
    return (
      <>
        <Note tone="amber" icon="alert" className="mb8">{data.reason}</Note>
        <DownloadButton path={path} />
      </>
    );
  }
  return (
    <div>
      <PreviewHeader path={path} />
      {data.anchor_found === false ? (
        <Note tone="amber" icon="alert" className="mb8">位置「{anchorLabel(anchor)}」在当前文件中已不存在（资料可能已被替换），以下显示文档开头</Note>
      ) : null}
      {data.has_more_before || data.has_more_after ? (
        <Note tone="blue" icon="info" className="mb8">仅显示定位点附近的 {data.segments.length} 段（共 {data.total} 段），完整内容请下载原文件</Note>
      ) : null}
      <div className="evd-paper src-preview" ref={bodyRef}>
        {blocks.map((block) => (block.type === 'table' ? (
          <SegmentTable key={block.cells[0].segment.anchor} block={block} target={hit} />
        ) : (
          <div key={block.segment.anchor} data-anchor={block.segment.anchor} className={cx('src-seg', block.segment.anchor === hit && 'src-hit')}>
            <span className="src-seg-no">{segmentNumber(block.segment.anchor)}</span>
            <div className="src-seg-text">
              {block.segment.text}
              {block.segment.truncated ? <span className="muted">……（本段过长，完整内容请下载原文件）</span> : null}
            </div>
          </div>
        )))}
      </div>
    </div>
  );
}

// PDF、图片：主服务按原样输出文件（F-04），浏览器打开；PDF 的 page:N 锚点跳到那一页
function FilePreview({ path, kind, anchor }) {
  const { services } = useApp();
  const { pid } = useProject();
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let alive = true;
    let made = null;
    services.fileUrl(rawPath(pid, path)).then((u) => {
      made = u.startsWith('blob:') ? u : null;
      if (alive) setUrl(u);
    }).catch(() => alive && setUrl(''));
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [services, pid, path]);
  const page = /^page:(\d+)$/.exec(anchor || '');
  return (
    <div>
      <PreviewHeader path={path} />
      {url === null ? <div className="evd-paper rs-loading"><span className="spin" />正在打开…</div> : null}
      {url && kind === 'pdf' ? <iframe className="src-frame" src={page ? `${url}#page=${page[1]}` : url} title={path} /> : null}
      {url && kind === 'image' ? <div className="src-image"><img src={url} alt={path.split('/').pop()} /></div> : null}
    </div>
  );
}

export default function SourcePreview({ path, kind, anchor = '', nonce = 0 }) {
  if (kind === 'segments') return <SegmentPreview path={path} anchor={anchor} nonce={nonce} />;
  if (kind === 'pdf' || kind === 'image') return <FilePreview path={path} kind={kind} anchor={anchor} />;
  return (
    <Empty icon="filetext" title={`${path.split('/').pop()} 不支持在线预览，可下载原文件查看`} actions={<DownloadButton path={path} />} />
  );
}
