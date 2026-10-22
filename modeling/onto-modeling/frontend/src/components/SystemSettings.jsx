import { ReloadOutlined, UploadOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  ConfigProvider,
  Descriptions,
  Input,
  List,
  Popconfirm,
  Segmented,
  Table,
  Typography,
  Upload,
  message,
} from 'antd';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { useRemainingHeight } from '../lib/useRemainingHeight.js';

const MODE_LABELS = {
  bypassPermissions: '全放行（默认）',
  default: '逐操作确认',
  acceptEdits: '仅自动接受编辑',
  plan: '计划模式',
};

export function RuntimeSettings({ project }) {
  const [runtime, setRuntime] = useState(null);

  useEffect(() => {
    api.getRuntime().then(setRuntime).catch(() => {});
  }, []);

  if (!runtime) return null;
  return (
    <div>
      <Typography.Paragraph strong style={{ marginBottom: 8 }}>
        工具权限模式
      </Typography.Paragraph>
      <Segmented
        block
        value={runtime.permission_mode}
        onChange={async (mode) => {
          try {
            const saved = await api.saveRuntime(mode);
            setRuntime({ ...runtime, ...saved });
            message.success('已保存，新会话生效');
          } catch (error) {
            message.error(error.message);
          }
        }}
        options={runtime.allowed.map((mode) => ({
          value: mode,
          label: MODE_LABELS[mode] || mode,
        }))}
      />
      <Typography.Paragraph type="secondary" style={{ marginTop: 12, fontSize: 12 }}>
        「逐操作确认」下每个工具调用都会在对话里弹确认卡。配置在新会话生效。
        {project && (
          <Typography.Link
            onClick={async () => {
              await api.closeSession(project.id).catch(() => {});
              message.success('会话已重置');
            }}
          >
            {' '}
            重启当前任务的会话
          </Typography.Link>
        )}
      </Typography.Paragraph>
    </div>
  );
}

export function SandboxStatus() {
  const [status, setStatus] = useState(null);
  const [checking, setChecking] = useState(false);
  const [keyword, setKeyword] = useState('');

  const check = async () => {
    setChecking(true);
    try {
      setStatus(await api.getSandboxStatus());
    } catch (error) {
      message.error(error.message);
    } finally {
      setChecking(false);
    }
  };
  useEffect(() => {
    check();
  }, []);

  const packages = status?.packages || [];
  const query = keyword.trim().toLowerCase();
  const shown = query ? packages.filter((item) => item.name.toLowerCase().includes(query)) : packages;
  let title = '正在检测 onto-agent-jiuwenbox…';
  let description;
  if (status?.connected) {
    title = '已连接 onto-agent-jiuwenbox';
    description = '代码生成时，脚本运行验证在沙箱中执行。';
  } else if (status) {
    title = '未连接 onto-agent-jiuwenbox';
    description = `${status.detail}。脚本运行验证将改在 onto-modeling 容器内执行。`;
  }

  return (
    <div>
      <Alert
        type={!status ? 'info' : status.connected ? 'success' : 'warning'}
        showIcon
        message={title}
        description={description}
        action={
          <Button size="small" icon={<ReloadOutlined />} loading={checking} onClick={check}>
            重新检测
          </Button>
        }
        style={{ marginBottom: 16 }}
      />
      {status && (
        <Descriptions column={2} size="small" bordered style={{ marginBottom: 20 }}>
          <Descriptions.Item label="验证用 Python">
            <Typography.Text code>{status.path}</Typography.Text> {status.python || ''}
          </Descriptions.Item>
          <Descriptions.Item label="pip">
            {status.connected ? status.pip || '未安装（按包元数据列出）' : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="脚本验证网关" span={2}>
            <Typography.Text code>{status.gateway || '未配置'}</Typography.Text>
            {status.socket ? (
              <Typography.Text type="secondary">
                {'  经 '}
                <Typography.Text code>{status.socket}</Typography.Text>
                {' 连接 jiuwenbox'}
              </Typography.Text>
            ) : null}
          </Descriptions.Item>
          <Descriptions.Item label="检测时间" span={2}>
            {new Date(status.checked_at).toLocaleString('zh-CN', { hour12: false })}
          </Descriptions.Item>
        </Descriptions>
      )}
      {status?.connected && (
        <>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              marginBottom: 8,
            }}
          >
            <Typography.Title level={5} style={{ margin: 0 }}>
              沙箱验证环境的 Python 包
              <Typography.Text type="secondary" style={{ fontSize: 13, fontWeight: 'normal' }}>
                {query ? `（匹配 ${shown.length} / ${packages.length}）` : `（共 ${packages.length} 个）`}
              </Typography.Text>
            </Typography.Title>
            <Input.Search
              allowClear
              placeholder="按包名筛选"
              onChange={(event) => setKeyword(event.target.value)}
              style={{ width: 220 }}
            />
          </div>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 12, fontSize: 12 }}>
            {`代码生成的脚本运行验证在沙箱里用 ${status.path} 执行，与 consumer 执行脚本的方式相同，脚本只能 import 下列包和 Python 标准库。`}
            {'沙箱禁止出网，查数脚本请用合成输入验证。'}
          </Typography.Paragraph>
          <SandboxPackageTable packages={shown} layoutKey={status} />
        </>
      )}
    </div>
  );
}

const PACKAGE_COLUMNS = [
  // 包名列定宽：版本号紧挨包名，不用横跨整行去对照
  { title: '包名', dataIndex: 'name', width: 320 },
  {
    title: '版本',
    dataIndex: 'version',
    render: (version) => <Typography.Text code>{version}</Typography.Text>,
  },
];
// 网格表格的单元格比小号默认值略宽松，行与列之间留出间隔
const PACKAGE_TABLE_THEME = { components: { Table: { cellPaddingBlockSM: 9, cellPaddingInlineSM: 14 } } };

/**
 * 带边框的包列表：向下延伸到页面底部，包更多时只滚动表体，表头和外框保持完整。
 * layoutKey 变化表示上方内容变了、表格位置会移动，需要重算高度。
 */
function SandboxPackageTable({ packages, layoutKey }) {
  const box = useRef(null);
  const available = useRemainingHeight(box, { deps: [layoutKey] });
  // scroll.y 只限制表体，表头和上下边框另占高度，按实际渲染结果量出来
  const [chromeHeight, setChromeHeight] = useState(42);
  useLayoutEffect(() => {
    const body = box.current?.querySelector('.ant-table-body');
    if (body) setChromeHeight(box.current.offsetHeight - body.offsetHeight);
  }, [layoutKey]);

  return (
    <div ref={box} className="sandbox-package-table">
      <ConfigProvider theme={PACKAGE_TABLE_THEME}>
        <Table
          bordered
          size="small"
          rowKey="name"
          dataSource={packages}
          columns={PACKAGE_COLUMNS}
          pagination={false}
          scroll={{ y: Math.max(120, available - chromeHeight) }}
          locale={{ emptyText: '没有匹配的包' }}
        />
      </ConfigProvider>
    </div>
  );
}

export function AssetsSettings() {
  const [info, setInfo] = useState(null);
  const [backups, setBackups] = useState([]);

  const refresh = () => {
    api.getAssets().then(setInfo).catch(() => {});
    api.assetBackups().then((data) => setBackups(data.backups)).catch(() => {});
  };
  useEffect(refresh, []);

  if (!info) return null;
  return (
    <div>
      {info.present ? (
        <Typography.Paragraph>
          当前资产：{info.commands} 个命令 · {info.agents} 个子代理 · {info.scripts} 个脚本
          <br />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            最近更新：{info.updated_at ? new Date(info.updated_at * 1000).toLocaleString() : '—'}
          </Typography.Text>
        </Typography.Paragraph>
      ) : (
        <Alert type="warning" showIcon message="资产目录缺失" style={{ marginBottom: 12 }} />
      )}
      <Upload
        accept=".zip"
        showUploadList={false}
        beforeUpload={async (file) => {
          try {
            await api.uploadAssets(file);
            refresh();
            message.success('资产已更新，旧版本已备份；新会话生效');
          } catch (error) {
            message.error(error.message);
          }
          return false;
        }}
      >
        <Button icon={<UploadOutlined />}>上传新资产包（zip）</Button>
      </Upload>
      <Typography.Paragraph type="secondary" style={{ margin: '12px 0', fontSize: 12 }}>
        资产包需包含 commands/ 与 agents/ 目录（可整包 .claude/ 或其内容）。
      </Typography.Paragraph>
      <Typography.Paragraph strong style={{ marginBottom: 4 }}>
        历史版本
      </Typography.Paragraph>
      <List
        size="small"
        dataSource={backups}
        locale={{ emptyText: '暂无备份版本' }}
        renderItem={(item) => (
          <List.Item
            actions={[
              <Popconfirm
                key="rb"
                title="回滚到该版本？当前资产会先自动备份。"
                onConfirm={async () => {
                  try {
                    await api.assetRollback(item.name);
                    refresh();
                    message.success('已回滚，新会话生效');
                  } catch (error) {
                    message.error(error.message);
                  }
                }}
              >
                <Button size="small" type="text">
                  回滚
                </Button>
              </Popconfirm>,
            ]}
          >
            <span>
              {new Date(item.time * 1000).toLocaleString()}
              <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                {item.commands} 个命令
              </Typography.Text>
            </span>
          </List.Item>
        )}
      />
    </div>
  );
}
