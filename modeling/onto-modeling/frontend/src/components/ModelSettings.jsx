import { Alert, Button, Form, Input, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

export default function ModelSettings({ project }) {
  const [form] = Form.useForm();
  const [current, setCurrent] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api
      .getModelSettings()
      .then((data) => {
        setCurrent(data);
        form.setFieldsValue({ base_url: data.base_url, model: data.model, api_key: '' });
      })
      .catch(() => {});
  }, [form]);

  const save = async (values) => {
    setSaving(true);
    try {
      const saved = await api.saveModelSettings(values);
      setCurrent(saved);
      form.setFieldsValue({ api_key: '' });
      message.success('已保存，新会话生效');
    } catch (error) {
      message.error(error.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      {current && !current.configured && (
        <Alert
          type="info"
          showIcon
          message="当前使用部署机的默认模型配置；此处保存后将优先生效"
          style={{ marginBottom: 12 }}
        />
      )}
      <Form form={form} layout="vertical" onFinish={save}>
        <Form.Item label="网关地址（Base URL）" name="base_url">
          <Input placeholder="https://gateway.example.com" />
        </Form.Item>
        <Form.Item
          label={`API Key${current?.api_key_masked ? `（当前：${current.api_key_masked}）` : ''}`}
          name="api_key"
          extra="留空表示不修改已保存的 Key；Key 仅存于服务端，界面只显示掩码"
        >
          <Input.Password placeholder="粘贴新的 API Key" autoComplete="new-password" />
        </Form.Item>
        <Form.Item label="模型（可选）" name="model" extra="留空使用服务默认模型">
          <Input placeholder="模型标识" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={saving}>
          保存
        </Button>
      </Form>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12, fontSize: 12 }}>
        配置在新会话生效。
        {project && (
          <Typography.Link
            onClick={async () => {
              await api.closeSession(project.id).catch(() => {});
              message.success('会话已重置，下次发消息按最新配置启动');
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
