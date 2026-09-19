import { UploadOutlined } from '@ant-design/icons';
import { Alert, Button, List, Popconfirm, Segmented, Typography, Upload, message } from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

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
