import { Button, Input, Space } from 'antd';
import { studioUpdatedRange, studioUpdatedRangeKey } from '../lib/studioTable.js';

/** Studio 表格「最后更新时间」列的日期范围筛选，作为 antd 列的 filterDropdown 使用。 */
export default function StudioUpdatedRangeFilter({ selectedKeys, setSelectedKeys, confirm, clearFilters }) {
  const { start, end } = studioUpdatedRange(selectedKeys[0]);
  const change = (next) => {
    const key = studioUpdatedRangeKey(next.start, next.end);
    setSelectedKeys(key ? [key] : []);
  };
  return (
    <div style={{ padding: 8 }} onKeyDown={(event) => event.stopPropagation()}>
      <Space direction="vertical" size={6}>
        <Input
          size="small"
          type="date"
          addonBefore="从"
          aria-label="起始日期"
          value={start}
          max={end || undefined}
          onChange={(event) => change({ start: event.target.value, end })}
        />
        <Input
          size="small"
          type="date"
          addonBefore="到"
          aria-label="截止日期"
          value={end}
          min={start || undefined}
          onChange={(event) => change({ start, end: event.target.value })}
        />
        <Space>
          <Button size="small" onClick={() => clearFilters?.({ confirm: true, closeDropdown: true })}>
            重置
          </Button>
          <Button size="small" type="primary" onClick={() => confirm()}>
            确定
          </Button>
        </Space>
      </Space>
    </div>
  );
}
