import { DownloadOutlined } from '@ant-design/icons';
import { Alert, Button, Empty, Space, Spin, Tabs, Typography } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { locateSheetCell } from '../lib/sourceLocation.js';

function columnLetter(index) {
  let label = '';
  let n = index;
  do {
    label = String.fromCharCode(65 + (n % 26)) + label;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return label;
}

function SheetTable({ sheet, target, nonce }) {
  const scrollRef = useRef(null);
  // 合并单元格：起点渲染 rowSpan/colSpan，被覆盖的格子跳过
  const { anchors, covered } = useMemo(() => {
    const anchorMap = new Map();
    const coveredSet = new Set();
    for (const m of sheet.merges || []) {
      anchorMap.set(`${m.row}:${m.col}`, m);
      for (let r = m.row; r < m.row + m.rowspan; r += 1) {
        for (let c = m.col; c < m.col + m.colspan; c += 1) {
          if (r !== m.row || c !== m.col) coveredSet.add(`${r}:${c}`);
        }
      }
    }
    return { anchors: anchorMap, covered: coveredSet };
  }, [sheet]);

  // 定位格落在合并区内时高亮整个合并区（即合并区左上角那个格子）
  const hitKey = useMemo(() => {
    if (!target) return '';
    const merge = (sheet.merges || []).find(
      (m) =>
        target.row >= m.row && target.row < m.row + m.rowspan &&
        target.col >= m.col && target.col < m.col + m.colspan,
    );
    return merge ? `${merge.row}:${merge.col}` : `${target.row}:${target.col}`;
  }, [sheet, target]);

  useEffect(() => {
    if (!hitKey || !scrollRef.current) return;
    const cell = [...scrollRef.current.querySelectorAll('[data-cell]')].find(
      (node) => node.dataset.cell === hitKey,
    );
    cell?.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
  }, [hitKey, nonce]);

  const colCount = Math.max(0, ...sheet.rows.map((r) => r.length));
  if (sheet.rows.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空工作表" />;
  }

  return (
    <div className="excel-scroll" ref={scrollRef}>
      <table className="excel-table">
        <thead>
          <tr>
            <th className="excel-corner" />
            {Array.from({ length: colCount }, (_, c) => (
              <th key={c}>{columnLetter(c)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sheet.rows.map((row, r) => (
            <tr key={r}>
              <th className="excel-rowno">{r + 1}</th>
              {Array.from({ length: colCount }, (_, c) => {
                if (covered.has(`${r}:${c}`)) return null;
                const merge = anchors.get(`${r}:${c}`);
                const value = row[c] ?? '';
                const numeric = value !== '' && !Number.isNaN(Number(value));
                return (
                  <td
                    key={c}
                    data-cell={`${r}:${c}`}
                    rowSpan={merge?.rowspan}
                    colSpan={merge?.colspan}
                    className={
                      [numeric && 'num', hitKey === `${r}:${c}` && 'source-hit'].filter(Boolean).join(' ') ||
                      undefined
                    }
                  >
                    {value}
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

export default function ExcelView({ project, path, anchor = '', nonce = 0 }) {
  const [data, setData] = useState(null);
  const [activeKey, setActiveKey] = useState('0');

  useEffect(() => {
    setData(null);
    api
      .excelPreview(project.id, path)
      .then(setData)
      .catch((error) => setData({ error: error.message }));
  }, [project.id, path]);

  const located = useMemo(() => (data?.sheets ? locateSheetCell(data, anchor) : null), [data, anchor]);

  // 打开或再次点击定位芯片时切到目标工作表
  useEffect(() => {
    setActiveKey(located?.index != null ? `${located.index}` : '0');
  }, [located, nonce]);

  if (!data) return <Spin style={{ marginTop: 48 }} />;
  if (data.error) {
    return (
      <Space direction="vertical">
        <Alert type="warning" showIcon message={data.error} />
        <Button icon={<DownloadOutlined />} href={api.rawUrl(project.id, path)} download={path.split('/').pop()}>
          下载原文件
        </Button>
      </Space>
    );
  }

  const truncated = data.sheets.some((s) => s.truncated) || data.sheet_truncated;
  return (
    <div>
      <Space style={{ marginBottom: 8 }} align="center" wrap>
        <Typography.Title level={5} style={{ margin: 0 }}>
          {path}
        </Typography.Title>
        <Button size="small" icon={<DownloadOutlined />} href={api.rawUrl(project.id, path)} download={path.split('/').pop()}>
          下载原文件
        </Button>
      </Space>
      {truncated && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 8 }}
          message="表格较大，仅预览前 500 行 × 60 列；完整内容请下载原文件"
        />
      )}
      {located?.problem && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={located.problem === 'range' ? `${located.message}，完整内容请下载原文件` : located.message}
        />
      )}
      <Tabs
        size="small"
        activeKey={activeKey}
        onChange={setActiveKey}
        items={data.sheets.map((sheet, index) => ({
          key: `${index}`,
          label: sheet.name,
          children: (
            <SheetTable
              sheet={sheet}
              target={located?.index === index ? located.cell : null}
              nonce={nonce}
            />
          ),
        }))}
      />
    </div>
  );
}
