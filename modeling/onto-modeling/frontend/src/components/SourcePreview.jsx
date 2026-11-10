import { DownloadOutlined } from '@ant-design/icons';
import { Alert, Button, Empty, Space, Spin, Typography } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { anchorLabel, groupSegments } from '../lib/sourceLocation.js';

// 原始资料只读预览：resource/ 下的资料不提供编辑入口
export function DownloadButton({ project, path, size = 'small' }) {
  return (
    <Button
      size={size}
      icon={<DownloadOutlined />}
      href={api.rawUrl(project.id, path)}
      download={path.split('/').pop()}
    >
      下载原文件
    </Button>
  );
}

function PreviewHeader({ project, path, extra }) {
  return (
    <Space style={{ marginBottom: 8 }} align="center" wrap>
      <Typography.Title level={5} style={{ margin: 0 }}>
        {path}
      </Typography.Title>
      <DownloadButton project={project} path={path} />
      {extra}
    </Space>
  );
}

// 行号/段号列只显示编号本身；表格单元格在表内高亮，不进编号列
const segmentNumber = (anchor) => anchor.replace(/^(L|p:)/, '');

function SegmentTable({ block, target }) {
  const cells = new Map(block.cells.map((c) => [`${c.row}:${c.cell}`, c.segment]));
  return (
    <div className="source-table-wrap">
      <div className="source-table-title">表 {block.table}</div>
      <table className="source-table">
        <tbody>
          {Array.from({ length: block.rows }, (_, r) => (
            <tr key={r}>
              {Array.from({ length: block.cols }, (_, c) => {
                const segment = cells.get(`${r + 1}:${c + 1}`);
                const anchor = `table:${block.table}/row:${r + 1}/cell:${c + 1}`;
                return (
                  <td key={c} data-anchor={anchor} className={anchor === target ? 'source-hit' : undefined}>
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

function SegmentPreview({ project, path, anchor, nonce }) {
  const [data, setData] = useState(null);
  const bodyRef = useRef(null);

  useEffect(() => {
    setData(null);
    api
      .sourcePreview(project.id, path, anchor)
      .then(setData)
      .catch((error) => setData({ error: error.message }));
  }, [project.id, path, anchor]);

  const blocks = useMemo(() => groupSegments(data?.segments), [data]);
  const hit = data?.anchor_found ? anchor : '';

  // 再次点击同一位置时 nonce 变化，重新滚动到定位点
  useEffect(() => {
    if (!hit || !bodyRef.current) return;
    const element = [...bodyRef.current.querySelectorAll('[data-anchor]')].find(
      (node) => node.dataset.anchor === hit,
    );
    element?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [hit, data, nonce]);

  if (!data) return <Spin style={{ marginTop: 48 }} />;
  if (data.error) {
    return (
      <Space direction="vertical">
        <Alert type="warning" showIcon message={data.error} />
        {data.error !== '文件不存在' && <DownloadButton project={project} path={path} />}
      </Space>
    );
  }
  if (data.status !== 'ok') {
    return (
      <Space direction="vertical">
        <Alert type="warning" showIcon message={data.reason} />
        <DownloadButton project={project} path={path} />
      </Space>
    );
  }

  return (
    <div>
      <PreviewHeader project={project} path={path} />
      {data.anchor_found === false && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={`位置「${anchorLabel(anchor)}」在当前文件中已不存在（资料可能已被替换），以下显示文档开头`}
        />
      )}
      {(data.has_more_before || data.has_more_after) && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message={`仅显示定位点附近的 ${data.segments.length} 段（共 ${data.total} 段），完整内容请下载原文件`}
        />
      )}
      <div className="source-preview" ref={bodyRef}>
        {blocks.map((block) =>
          block.type === 'table' ? (
            <SegmentTable key={block.cells[0].segment.anchor} block={block} target={hit} />
          ) : (
            <div
              key={block.segment.anchor}
              data-anchor={block.segment.anchor}
              className={`source-seg${block.segment.anchor === hit ? ' source-hit' : ''}`}
            >
              <span className="source-seg-no">{segmentNumber(block.segment.anchor)}</span>
              <div className="source-seg-text">
                {block.segment.text}
                {block.segment.truncated && (
                  <Typography.Text type="secondary">……（本段过长，完整内容请下载原文件）</Typography.Text>
                )}
              </div>
            </div>
          ),
        )}
      </div>
    </div>
  );
}

export default function SourcePreview({ project, path, kind, anchor = '', nonce = 0 }) {
  if (kind === 'segments') {
    return <SegmentPreview project={project} path={path} anchor={anchor} nonce={nonce} />;
  }
  const url = api.rawUrl(project.id, path);
  if (kind === 'pdf') {
    return (
      <div>
        <PreviewHeader project={project} path={path} />
        <iframe className="source-frame" src={url} title={path} />
      </div>
    );
  }
  if (kind === 'image') {
    return (
      <div>
        <PreviewHeader project={project} path={path} />
        <div className="source-image">
          <img src={url} alt={path.split('/').pop()} />
        </div>
      </div>
    );
  }
  return (
    <Empty description={`${path.split('/').pop()} 不支持在线预览，可下载原文件查看`}>
      <DownloadButton project={project} path={path} size="middle" />
    </Empty>
  );
}
