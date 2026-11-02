import {
  BgColorsOutlined,
  ExportOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Button, Space, Tooltip, Typography } from 'antd';
import { useState } from 'react';
import { api } from '../api/files.js';

/** HTML 报告预览：沙箱 iframe + 工具条（刷新 / 新窗口 / 底色切换）。 */
export default function HtmlPreview({ project, path }) {
  const [nonce, setNonce] = useState(0);
  const [lightBg, setLightBg] = useState(true);
  const url = api.rawUrl(project.id, path);

  return (
    <div className="html-preview-root">
      <Space style={{ marginBottom: 8 }} align="center" wrap>
        <Typography.Title level={5} style={{ margin: 0 }}>
          {path}
        </Typography.Title>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => setNonce(nonce + 1)}>
          刷新
        </Button>
        <Button size="small" icon={<ExportOutlined />} href={url} target="_blank">
          新窗口打开
        </Button>
        <Tooltip title="切换预览底色（透明背景的报告用）">
          <Button size="small" icon={<BgColorsOutlined />} onClick={() => setLightBg(!lightBg)} />
        </Tooltip>
      </Space>
      <iframe
        key={nonce}
        className="html-preview"
        style={{ background: lightBg ? '#ffffff' : '#1c2330' }}
        title={path}
        sandbox="allow-scripts"
        src={url}
      />
    </div>
  );
}
