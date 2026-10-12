import { Alert, Button, Divider, Form, Input, Radio, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

export default function ModelSettings({ project }) {
  const [form] = Form.useForm();
  const [jiuwenForm] = Form.useForm();
  const [current, setCurrent] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savingJiuwen, setSavingJiuwen] = useState(false);
  const [savingEngine, setSavingEngine] = useState(false);

  useEffect(() => {
    api
      .getModelSettings()
      .then((data) => {
        setCurrent(data);
        form.setFieldsValue({ base_url: data.base_url, model: data.model, api_key: '' });
        jiuwenForm.setFieldsValue({
          base_url: data.jiuwen?.base_url || '',
          model: data.jiuwen?.model || '',
          api_key: '',
        });
      })
      .catch(() => {});
  }, [form, jiuwenForm]);

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

  const saveJiuwen = async (values) => {
    setSavingJiuwen(true);
    try {
      const saved = await api.saveJiuwenModelSettings(values);
      setCurrent(saved);
      jiuwenForm.setFieldsValue({ api_key: '' });
      message.success('已保存，内测引擎的新会话生效');
    } catch (error) {
      message.error(error.message);
    } finally {
      setSavingJiuwen(false);
    }
  };

  const saveDefaultEngine = async (engine) => {
    setSavingEngine(true);
    try {
      setCurrent(await api.saveDefaultEngine(engine));
      message.success('已保存；此后新建或导入的项目使用该引擎');
    } catch (error) {
      message.error(error.message);
    } finally {
      setSavingEngine(false);
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
      <Typography.Title level={5} style={{ marginTop: 0 }}>
        默认引擎
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
        此后新建或导入的项目都用这个引擎；已有项目不受影响，可在项目卡片的「更多 → 会话引擎」单独切换。
      </Typography.Paragraph>
      <Radio.Group
        value={current?.default_engine}
        disabled={savingEngine || !current}
        onChange={(e) => saveDefaultEngine(e.target.value)}
        style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
      >
        <Radio value="claude">稳定引擎 — 功能最完整</Radio>
        <Radio value="jiuwen" disabled={!current?.engines?.jiuwen}>
          内测引擎 — OpenAI 兼容模型直连，低内存
          {current && !current.engines?.jiuwen && '（当前部署未安装）'}
        </Radio>
      </Radio.Group>

      <Divider />
      <Typography.Title level={5}>
        Anthropic 网关（稳定引擎）
      </Typography.Title>
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

      <Divider />
      <Typography.Title level={5}>OpenAI 兼容（内测引擎）</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
        供切换到内测引擎的项目使用，可直连 OpenAI 兼容协议的模型服务（如 Qwen /
        DeepSeek / GLM）。引擎在项目卡片的「更多」菜单中切换。
      </Typography.Paragraph>
      <Form form={jiuwenForm} layout="vertical" onFinish={saveJiuwen}>
        <Form.Item label="网关地址（Base URL）" name="base_url">
          <Input placeholder="https://api.example.com/v1" />
        </Form.Item>
        <Form.Item
          label={`API Key${
            current?.jiuwen?.api_key_masked ? `（当前：${current.jiuwen.api_key_masked}）` : ''
          }`}
          name="api_key"
          extra="留空表示不修改已保存的 Key"
        >
          <Input.Password placeholder="粘贴新的 API Key" autoComplete="new-password" />
        </Form.Item>
        <Form.Item label="模型" name="model">
          <Input placeholder="模型标识，如 qwen-max / glm-5.2" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={savingJiuwen}>
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
