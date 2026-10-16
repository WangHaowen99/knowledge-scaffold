import { Alert, Button, Divider, Form, Input, Radio, Typography, message } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import {
  PRESET_ABSENT_HINT,
  PRESET_MISSED,
  contextPayload,
  defaultValuesOf,
  filledHint,
  presetChange,
  presetSuggestion,
  presetValues,
  restoredHint,
} from '../lib/contextSettings.js';
import ModelContextSettings from './ModelContextSettings.jsx';

// 模型名停止输入后再查推荐表
const PRESET_LOOKUP_DELAY = 400;

// 已保存的模型不在推荐表中，或命中推荐表、且生效值与推荐值不同时的提示
const savedHint = (preset, data) =>
  data?.model && !preset
    ? PRESET_ABSENT_HINT
    : presetSuggestion(preset, data?.context, defaultValuesOf(data?.context_defaults));

export default function ModelSettings({ project }) {
  const [form] = Form.useForm();
  const [contextForm] = Form.useForm();
  const [jiuwenForm] = Form.useForm();
  const [current, setCurrent] = useState(null);
  const [saving, setSaving] = useState(false);
  const [savingJiuwen, setSavingJiuwen] = useState(false);
  const [savingEngine, setSavingEngine] = useState(false);
  // 推荐表：当前模型名匹配到的条目 id、查表提示、预填后尚未保存的上下文参数
  const [presetHint, setPresetHint] = useState(null);
  const matchedPresetId = useRef(null);
  const contextPending = useRef(false);
  const lookupTimer = useRef(null);
  const lookupSeq = useRef(0);

  useEffect(() => {
    const seq = lookupSeq.current;
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
        if (!data.model) return null;
        // 打开页面不自动填：只记下已保存模型匹配的条目，与推荐值不同时给出提示
        return api.matchModelContextPreset(data.model).then(({ preset }) => {
          if (seq !== lookupSeq.current) return;
          matchedPresetId.current = preset?.id ?? null;
          setPresetHint(savedHint(preset, data));
        });
      })
      .catch(() => {});
  }, [form, jiuwenForm]);

  useEffect(() => () => clearTimeout(lookupTimer.current), []);

  const fillPreset = (preset) => {
    contextForm.setFieldsValue(presetValues(preset));
    contextPending.current = true;
    setPresetHint(filledHint(preset));
  };

  // 模型名改回已保存的模型：推荐表填入或清空过的上下文参数恢复成已保存的值，
  // 避免只想改 Key 时顺手碰了模型名，保存后把已保存的上下文参数冲掉；恢复了就提示，免得误以为是推荐表给的
  const restoreSaved = (model, preset) => {
    const restored = contextPending.current;
    if (restored) {
      contextForm.setFieldsValue(contextPayload(current?.context));
      contextPending.current = false;
    }
    matchedPresetId.current = preset?.id ?? null;
    setPresetHint(
      restored
        ? restoredHint(model, preset, current?.context, defaultValuesOf(current?.context_defaults))
        : savedHint(preset, current),
    );
  };

  const lookupPreset = (model) => {
    clearTimeout(lookupTimer.current);
    const seq = ++lookupSeq.current;
    const isSavedModel = model === (current?.model || '').trim();
    if (!model) {
      if (isSavedModel) {
        restoreSaved(model, null);
      } else {
        matchedPresetId.current = null;
        setPresetHint(null);
      }
      return;
    }
    lookupTimer.current = setTimeout(async () => {
      let preset;
      try {
        ({ preset } = await api.matchModelContextPreset(model));
      } catch {
        return;
      }
      if (seq !== lookupSeq.current) return;
      if (isSavedModel) {
        restoreSaved(model, preset);
        return;
      }
      const change = presetChange(preset, matchedPresetId.current);
      if (!change) return;
      matchedPresetId.current = change.matchedId;
      if (change.fill) {
        contextForm.setFieldsValue(change.fill);
        contextPending.current = true;
      }
      setPresetHint(change.hint);
    }, PRESET_LOOKUP_DELAY);
  };

  // 推荐表在弹窗里改过（含停用）或保存后：按输入框里的模型名重新查表，只更新提示、不改输入框里的值；
  // 推荐值与输入框不同时提示，可点「填入推荐值」。未命中时提示表中没有该模型，并保留「已清空过」的状态，
  // 免得随后输入又清空手填的值
  const recheckPreset = async () => {
    clearTimeout(lookupTimer.current);
    const seq = ++lookupSeq.current;
    const model = (form.getFieldValue('model') || '').trim();
    if (!model) return;
    let preset;
    try {
      ({ preset } = await api.matchModelContextPreset(model));
    } catch {
      return;
    }
    if (seq !== lookupSeq.current) return;
    if (preset || matchedPresetId.current !== PRESET_MISSED) matchedPresetId.current = preset?.id ?? null;
    setPresetHint(
      preset
        ? presetSuggestion(preset, contextForm.getFieldsValue(), defaultValuesOf(current?.context_defaults))
        : PRESET_ABSENT_HINT,
    );
  };

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
      contextPending.current = false;
      setPresetHint(null);
      setCurrent(saved);
      recheckPreset();
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
      <Form
        form={form}
        layout="vertical"
        onValuesChange={(changed) => {
          if ('model' in changed) lookupPreset((changed.model || '').trim());
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
              {presetHint && (
                <div>
                  <Typography.Text type={presetHint.type} style={{ fontSize: 12 }}>
                    {presetHint.text}
                  </Typography.Text>
                  {presetHint.preset && (
                    <Typography.Link
                      onClick={() => fillPreset(presetHint.preset)}
                      style={{ fontSize: 12, marginLeft: 8 }}
                    >
                      填入推荐值
                    </Typography.Link>
                  )}
                </div>
              )}
            </>
          }
        >
          <Input placeholder="模型标识" />
        </Form.Item>
      </Form>

      <Divider />
      <ModelContextSettings form={contextForm} current={current} onPresetsChanged={recheckPreset} />
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
