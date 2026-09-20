import { DownloadOutlined } from '@ant-design/icons';
import { Alert, Button, Empty, Space, Spin, Tabs, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api.js';

function columnLetter(index) {
  let label = '';
  let n = index;
  do {
    label = String.fromCharCode(65 + (n % 26)) + label;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return label;
}

function SheetTable({ sheet }) {
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

  const colCount = Math.max(0, ...sheet.rows.map((r) => r.length));
  if (sheet.rows.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="空工作表" />;
  }

  return (
    <div className="excel-scroll">
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
                    rowSpan={merge?.rowspan}
                    colSpan={merge?.colspan}
                    className={numeric ? 'num' : undefined}
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

export default function ExcelView({ project, path }) {
  const [data, setData] = useState(null);

  useEffect(() => {
    setData(null);
    api
      .excelPreview(project.id, path)
      .then(setData)
      .catch((error) => setData({ error: error.message }));
  }, [project.id, path]);

  if (!data) return <Spin style={{ marginTop: 48 }} />;
  if (data.error) {
    return (
      <Space direction="vertical">
        <Alert type="warning" showIcon message={data.error} />
        <Button icon={<DownloadOutlined />} href={api.rawUrl(project.id, path)}>
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
        <Button size="small" icon={<DownloadOutlined />} href={api.rawUrl(project.id, path)}>
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
      <Tabs
        size="small"
        items={data.sheets.map((sheet, index) => ({
          key: `${index}`,
          label: sheet.name,
          children: <SheetTable sheet={sheet} />,
        }))}
      />
    </div>
  );
}
