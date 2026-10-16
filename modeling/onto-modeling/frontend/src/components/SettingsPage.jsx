import { ArrowLeftOutlined } from '@ant-design/icons';
import {
  Button,
  Descriptions,
  Form,
  Input,
  Menu,
  Segmented,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { useContext, useEffect, useState } from 'react';
import { api, authStore } from '../lib/api.js';
import { ThemeCtx } from '../lib/theme-context.js';
import { ACCENTS } from '../theme.js';
import McpSettings from './McpSettings.jsx';
import ModelSettings from './ModelSettings.jsx';
import PersonalModelSettings from './PersonalModelSettings.jsx';
import { AssetsSettings, RuntimeSettings } from './SystemSettings.jsx';

const ACCENT_LABELS = {
  blue: '默认蓝',
  violet: '紫',
  teal: '青',
  orange: '橙',
  graphite: '石墨',
};

function AccountSection({ authEnabled }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  if (!authEnabled) {
    return <Typography.Paragraph type="secondary">当前部署未启用登录鉴权。</Typography.Paragraph>;
  }
  return (
    <div style={{ maxWidth: 420 }}>
      <Descriptions column={1} size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="用户名">{authStore.username}</Descriptions.Item>
        <Descriptions.Item label="角色">
          {authStore.role === 'admin' ? '管理员' : '建模者'}
        </Descriptions.Item>
      </Descriptions>
      <Typography.Paragraph strong>修改密码</Typography.Paragraph>
      <Form
        form={form}
        layout="vertical"
        onFinish={async (values) => {
          setSaving(true);
          try {
            await api.changePassword(authStore.username, values.old_password, values.new_password);
            form.resetFields();
            message.success('密码已修改，下次登录使用新密码');
          } catch (error) {
            message.error(error.message);
          } finally {
            setSaving(false);
          }
        }}
      >
        <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
          <Input.Password autoComplete="current-password" />
        </Form.Item>
        <Form.Item
          name="new_password"
          label="新密码"
          rules={[{ required: true, min: 8, message: '至少 8 位' }]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认新密码"
          dependencies={['new_password']}
          rules={[
            { required: true, message: '请再次输入' },
            ({ getFieldValue }) => ({
              validator: (_, value) =>
                value === getFieldValue('new_password')
                  ? Promise.resolve()
                  : Promise.reject(new Error('两次输入不一致')),
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={saving}>
          修改密码
        </Button>
      </Form>
    </div>
  );
}

function AppearanceSection() {
  const theme = useContext(ThemeCtx);
  return (
    <div style={{ maxWidth: 420 }}>
      <Typography.Paragraph strong style={{ marginBottom: 8 }}>
        明暗模式
      </Typography.Paragraph>
      <Segmented
        block
        value={theme.mode}
        onChange={theme.setMode}
        options={[
          { label: '浅色', value: 'light' },
          { label: '深色', value: 'dark' },
        ]}
        style={{ marginBottom: 24 }}
      />
      <Typography.Paragraph strong style={{ marginBottom: 8 }}>
        强调色
      </Typography.Paragraph>
      <div className="accent-row">
        {Object.keys(ACCENTS).map((key) => (
          <Tooltip key={key} title={ACCENT_LABELS[key] || key}>
            <button
              type="button"
              className={`accent-swatch${theme.accent === key ? ' active' : ''}`}
              style={{ background: ACCENTS[key].primary }}
              onClick={() => theme.setAccent(key)}
              aria-label={ACCENT_LABELS[key] || key}
            />
          </Tooltip>
        ))}
      </div>
    </div>
  );
}

const SECTION_TITLES = {
  account: '账号',
  appearance: '外观',
  model: '模型接入',
  myModel: '我的模型配置',
  runtime: '会话',
  assets: '提示词资产',
  mcp: 'MCP 服务器',
};

/** 设置中心（左 Menu + 右内容，与 consumer 设置页同构）。 */
export default function SettingsPage({
  project,
  isAdmin,
  authEnabled,
  initialSection = 'account',
  onBack,
  onOpenAdmin,
}) {
  const [section, setSection] = useState(initialSection);

  useEffect(() => {
    setSection(initialSection);
  }, [initialSection]);

  const items = [
    {
      type: 'group',
      label: '个人',
      children: [
        { key: 'account', label: '账号' },
        { key: 'appearance', label: '外观' },
        ...(authEnabled ? [{ key: 'myModel', label: '我的模型配置' }] : []),
      ],
    },
    ...(isAdmin
      ? [
          {
            type: 'group',
            label: '系统',
            children: [
              { key: 'model', label: '模型接入' },
              { key: 'runtime', label: '会话' },
              { key: 'assets', label: '提示词资产' },
            ],
          },
          {
            type: 'group',
            label: '管理',
            children: [{ key: 'admin', label: '运维中心' }],
          },
        ]
      : []),
    ...(project ? [{ type: 'group', label: '项目', children: [{ key: 'mcp', label: 'MCP 服务器' }] }] : []),
  ];

  return (
    <div className="settings-page">
      <div className="settings-side">
        <Button icon={<ArrowLeftOutlined />} onClick={onBack} style={{ margin: 12 }}>
          返回
        </Button>
        <Menu
          mode="inline"
          selectedKeys={[section]}
          items={items}
          onClick={({ key }) => {
            if (key === 'admin') onOpenAdmin?.();
            else setSection(key);
          }}
          style={{ borderInlineEnd: 'none' }}
        />
      </div>
      <div className="settings-body">
        <Typography.Title level={4} style={{ marginTop: 0 }}>
          {SECTION_TITLES[section]}
        </Typography.Title>
        {section === 'account' && <AccountSection authEnabled={authEnabled} />}
        {section === 'appearance' && <AppearanceSection />}
        {section === 'myModel' && authEnabled && <PersonalModelSettings />}
        {section === 'model' && isAdmin && <ModelSettings project={project} />}
        {section === 'runtime' && isAdmin && <RuntimeSettings project={project} />}
        {section === 'assets' && isAdmin && <AssetsSettings />}
        {section === 'mcp' && project && <McpSettings project={project} />}
      </div>
    </div>
  );
}
