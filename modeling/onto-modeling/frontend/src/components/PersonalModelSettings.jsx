import { Alert, Button, Divider, Form, Input, Space, Tag, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

const ENGINES = [
  {
    key: 'claude',
    title: 'Anthropic 网关（稳定引擎）',
    urlPlaceholder: 'https://gateway.example.com',
  },
  {
    key: 'jiuwen',
    title: 'OpenAI 兼容（内测引擎）',
    urlPlaceholder: 'https://api.example.com/v1',
  },
];

function EngineForm({ engine, data, disabled, onSaved }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);

  useEffect(() => {
    form.setFieldsValue({ base_url: data?.base_url || '', model: data?.model || '', api_key: '' });
  }, [form, data]);

  const save = async (values) => {
    setSaving(true);
    try {
      onSaved(await api.saveMyModelSettings(engine.key, values));
      form.setFieldsValue({ api_key: '' });
      message.success('已保存，你下一次发送消息时生效');
    } catch (error) {
      message.error(error.message);
    } finally {
      setSaving(false);
    }
  };

  const clear = async () => {
    setClearing(true);
    try {
      onSaved(await api.clearMyModelSettings(engine.key));
      message.success('已清除个人配置，改用管理员配置');
    } catch (error) {
      message.error(error.message);
    } finally {
      setClearing(false);
    }
  };

  const hasKey = Boolean(data?.api_key_masked);
  let fallbackSource = '使用部署默认配置';
  if (data?.admin_configured) fallbackSource = '使用管理员配置';
  return (
    <>
      <Typography.Title level={5}>
        <Space>
          {engine.title}
          {data?.active ? <Tag color="success">使用个人 Key</Tag> : <Tag>{fallbackSource}</Tag>}
        </Space>
      </Typography.Title>
      <Form form={form} layout="vertical" onFinish={save} disabled={disabled}>
        <Form.Item
          label={`API Key${hasKey ? '（已设置）' : ''}`}
          name="api_key"
          rules={hasKey ? [] : [{ required: true, message: '个人配置必须填写自己的 API Key' }]}
          extra={hasKey ? '留空表示保持个人 Key 不变' : 'Key 加密保存在服务端，界面不回显'}
        >
          <Input.Password placeholder="粘贴你的 API Key" autoComplete="new-password" />
        </Form.Item>
        <Form.Item label="网关地址（可选）" name="base_url" extra="留空沿用管理员配置的网关地址">
          <Input placeholder={engine.urlPlaceholder} />
        </Form.Item>
        <Form.Item label="模型（可选）" name="model" extra="留空沿用管理员配置的模型">
          <Input placeholder="模型标识" />
        </Form.Item>
        <Space>
          <Button type="primary" htmlType="submit" loading={saving}>
            保存
          </Button>
          {hasKey && (
            <Button danger onClick={clear} loading={clearing} disabled={false}>
              清除个人配置
            </Button>
          )}
        </Space>
      </Form>
    </>
  );
}

/** 个人模型配置：用自己的 API Key 覆盖管理员配置，只影响本人发起的会话。 */
export default function PersonalModelSettings() {
  const [current, setCurrent] = useState(null);

  useEffect(() => {
    api
      .getMyModelSettings()
      .then(setCurrent)
      .catch(() => {});
  }, []);

  const disabled = !current?.enabled;
  const policyOff = Boolean(current) && disabled;
  return (
    <div style={{ maxWidth: 520 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        填写后，你在任意项目里发送的消息都用你自己的 Key 调用模型；不填则使用管理员配置。
        同一项目有他人正在运行的轮次时，需等其结束后再发送。
      </Typography.Paragraph>
      {policyOff && (
        <Alert
          type="info"
          showIcon
          message="管理员已关闭个人 API Key，当前统一使用管理员配置"
          style={{ marginBottom: 12 }}
        />
      )}
      {ENGINES.map((engine, index) => (
        <div key={engine.key}>
          {index > 0 && <Divider />}
          <EngineForm
            engine={engine}
            data={current?.[engine.key]}
            disabled={disabled}
            onSaved={setCurrent}
          />
        </div>
      ))}
    </div>
  );
}
