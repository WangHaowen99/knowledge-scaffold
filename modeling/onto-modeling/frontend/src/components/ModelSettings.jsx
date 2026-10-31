import { Alert, Button, Divider, Form, Input, Radio, Switch, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { contextPayload, estimateJiuwenCompaction } from '../lib/contextSettings.js';
import ModelContextSettings from './ModelContextSettings.jsx';
import useContextPreset from './useContextPreset.js';

// 模型输入框下方的推荐表提示
function PresetHint({ hint, onFill }) {
  if (!hint) return null;
  return (
    <div>
      <Typography.Text type={hint.type} style={{ fontSize: 12 }}>
        {hint.text}
      </Typography.Text>
      {hint.preset && (
        <Typography.Link onClick={() => onFill(hint.preset)} style={{ fontSize: 12, marginLeft: 8 }}>
          填入推荐值
        </Typography.Link>
      )}
    </div>
  );
}

export default function ModelSettings({ project }) {
  const [form] = Form.useForm();
  const [contextForm] = Form.useForm();
  const [jiuwenForm] = Form.useForm();
  const [jiuwenContextForm] = Form.useForm();
  const [current, setCurrent] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savingJiuwen, setSavingJiuwen] = useState(false);
  const [savingEngine, setSavingEngine] = useState(false);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const claudePreset = useContextPreset(form, contextForm, current);
  const jiuwenPreset = useContextPreset(jiuwenForm, jiuwenContextForm, current?.jiuwen);
  // 推荐表两边共用，在任一处弹窗里改过都要两边重新查表
  const recheckPresets = () => {
    claudePreset.recheck();
    jiuwenPreset.recheck();
  };

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
        claudePreset.init(data);
        jiuwenPreset.init(data.jiuwen);
      })
      .catch(() => {});
    // 只在打开页面时取一次；init 只用到 ref 与 setState，取首次渲染的即可
  }, [form, jiuwenForm]);

  // 网关配置与上下文参数共用一个保存按钮：两边都校验通过才提交，服务端一起校验、一起写入
  const save = async () => {
    let values;
    let contextValues;
    try {
      [values, contextValues] = await Promise.all([form.validateFields(), contextForm.validateFields()]);
    } catch {
      message.error('配置有误，请按提示修正后再保存');
      return;
    }
    setSaving(true);
    try {
      const saved = await api.saveModelSettings({ ...values, context: contextPayload(contextValues) });
      form.setFieldsValue({ api_key: '' });
      claudePreset.afterSave();
      setCurrent(saved);
      message.success('已保存，新会话生效');
    } catch (error) {
      message.error(error.message);
    } finally {
      setSaving(false);
    }
  };

  const saveJiuwen = async () => {
    let values;
    let contextValues;
    try {
      [values, contextValues] = await Promise.all([
        jiuwenForm.validateFields(),
        jiuwenContextForm.validateFields(),
      ]);
    } catch {
      message.error('配置有误，请按提示修正后再保存');
      return;
    }
    setSavingJiuwen(true);
    try {
      const saved = await api.saveJiuwenModelSettings({ ...values, context: contextPayload(contextValues) });
      jiuwenForm.setFieldsValue({ api_key: '' });
      jiuwenPreset.afterSave();
      setCurrent(saved);
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

  const savePersonalPolicy = async (allow) => {
    setSavingPolicy(true);
    try {
      setCurrent(await api.saveModelPersonalPolicy(allow));
      message.success(allow ? '已允许成员使用个人 Key' : '已关闭个人 Key，全部会话改用本页配置');
    } catch (error) {
      message.error(error.message);
    } finally {
      setSavingPolicy(false);
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
      <Form
        form={form}
        layout="vertical"
        onValuesChange={(changed) => {
          if ('model' in changed) claudePreset.lookup((changed.model || '').trim());
        }}
      >
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
        <Form.Item
          label="模型（可选）"
          name="model"
          extra={
            <>
              留空使用服务默认模型；命中推荐表时自动填入下方上下文参数，未命中时清空
              <PresetHint hint={claudePreset.hint} onFill={claudePreset.fill} />
            </>
          }
        >
          <Input placeholder="模型标识" />
        </Form.Item>
      </Form>

      <Divider />
      <ModelContextSettings
        form={contextForm}
        title="上下文与自动压缩（稳定引擎）"
        saved={current?.context}
        contextDefaults={current?.context_defaults}
        onPresetsChanged={recheckPresets}
      />
      <Button type="primary" loading={saving} disabled={!current} onClick={save}>
        保存
      </Button>
      <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
        网关配置与上下文参数一起保存
      </Typography.Text>

      <Divider />
      <Typography.Title level={5}>OpenAI 兼容（内测引擎）</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
        供切换到内测引擎的项目使用，可直连 OpenAI 兼容协议的模型服务（如 Qwen /
        DeepSeek / GLM）。引擎在项目卡片的「更多」菜单中切换。
      </Typography.Paragraph>
      <Form
        form={jiuwenForm}
        layout="vertical"
        onValuesChange={(changed) => {
          if ('model' in changed) jiuwenPreset.lookup((changed.model || '').trim());
        }}
      >
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
        <Form.Item
          label="模型"
          name="model"
          extra={
            <>
              命中推荐表时自动填入下方上下文参数，未命中时清空
              <PresetHint hint={jiuwenPreset.hint} onFill={jiuwenPreset.fill} />
            </>
          }
        >
          <Input placeholder="模型标识，如 qwen-max / glm-5.2" />
        </Form.Item>
      </Form>

      <ModelContextSettings
        form={jiuwenContextForm}
        title="上下文与自动压缩（内测引擎）"
        saved={current?.jiuwen?.context}
        contextDefaults={current?.jiuwen?.context_defaults}
        estimate={estimateJiuwenCompaction}
        onPresetsChanged={recheckPresets}
      />
      <Button type="primary" loading={savingJiuwen} disabled={!current} onClick={saveJiuwen}>
        保存
      </Button>
      <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
        网关配置与上下文参数一起保存
      </Typography.Text>

      <Divider />
      <Typography.Title level={5}>个人 API Key</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
        开启后，成员可在「设置 → 我的模型配置」填写自己的 API Key，只用于本人发起的会话；
        未填写的成员继续使用本页配置。关闭后全部会话都使用本页配置。个人 Key
        的会话同样按本页的上下文与自动压缩参数运行。
      </Typography.Paragraph>
      <Switch
        checked={current?.allow_personal !== false}
        loading={savingPolicy}
        disabled={!current}
        onChange={savePersonalPolicy}
        checkedChildren="允许"
        unCheckedChildren="关闭"
      />

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
