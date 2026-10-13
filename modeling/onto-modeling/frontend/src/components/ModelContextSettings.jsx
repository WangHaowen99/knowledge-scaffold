import { Alert, Button, Form, InputNumber, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { estimateCompaction } from '../lib/contextUsage.js';

const FIELDS = [
  {
    name: 'max_context_tokens',
    label: '模型真实窗口',
    env: 'CLAUDE_CODE_MAX_CONTEXT_TOKENS',
    min: 1000,
    max: 10000000,
    placeholder: '如 1000000',
    help: '模型实际可接受的最大上下文（输入 + 输出），以网关报错中给出的上限为准。留空按默认窗口，模型窗口小于默认值时必须填写；只对引擎不认识的模型名生效，Claude 官方模型名按引擎内置窗口。',
  },
  {
    name: 'auto_compact_window',
    label: '压缩窗口',
    env: 'CLAUDE_CODE_AUTO_COMPACT_WINDOW',
    min: 100000,
    max: 1000000,
    placeholder: '100000 – 1000000',
    help: '自动压缩按这个容量计算阈值；实际取它与模型窗口中的较小值，只能调小。',
  },
  {
    name: 'max_output_tokens',
    label: '最大输出',
    env: 'CLAUDE_CODE_MAX_OUTPUT_TOKENS',
    min: 1000,
    max: 1000000,
    placeholder: '如 16000',
    help: '每次请求为模型输出预留的 token（max_tokens）。越大留给输入的越少；过小会截断长回答。',
  },
  {
    name: 'autocompact_pct',
    label: '触发百分比（%）',
    env: 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE',
    min: 1,
    max: 100,
    placeholder: '留空按默认阈值（有效窗口 − 13000）',
    help: '上下文用到有效窗口的该比例时触发压缩；只能比默认阈值更早，不能推后。',
  },
];

function Estimate({ values, defaultWindow }) {
  const result = estimateCompaction(values, defaultWindow);
  if (!result) {
    return <Alert type="info" showIcon message="填写模型真实窗口后显示阈值估算" />;
  }
  const fmt = (n) => n.toLocaleString();
  if (result.margin === undefined) {
    return (
      <Alert
        type="info"
        showIcon
        message={`触发阈值 ≈ ${fmt(result.threshold)}；未设置最大输出，按 CLI 默认预留，无法估算余量`}
      />
    );
  }
  const summary = `触发阈值 ≈ ${fmt(result.threshold)}，加最大输出 ${fmt(result.output)} 后余量 ${fmt(
    result.margin,
  )}（${(result.ratio * 100).toFixed(1)}%）`;
  if (result.margin <= 0) {
    return <Alert type="error" showIcon message={`${summary}：请求会在压缩前超出模型窗口`} />;
  }
  if (result.ratio < 0.1) {
    return <Alert type="warning" showIcon message={`${summary}：余量偏小，单轮较大的工具结果可能直接越过窗口`} />;
  }
  return <Alert type="success" showIcon message={summary} />;
}

export default function ModelContextSettings({ current, onSaved }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const values = Form.useWatch([], form);
  const defaults = current?.context_defaults || {};

  useEffect(() => {
    if (current?.context) form.setFieldsValue(current.context);
  }, [current, form]);

  const save = async (formValues) => {
    setSaving(true);
    try {
      const payload = Object.fromEntries(FIELDS.map(({ name }) => [name, formValues[name] ?? null]));
      onSaved(await api.saveModelContextSettings(payload));
      message.success('已保存，新会话生效');
    } catch (error) {
      message.error(error.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Typography.Title level={5}>上下文与自动压缩（稳定引擎）</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
        引擎按模型真实窗口计算自动压缩点；留空时按默认窗口
        {defaults.max_context_tokens ? `（${defaults.max_context_tokens}）` : ''}计算。
        接入窗口小于默认值的模型时必须填写，否则引擎会在自动压缩触发之前超出模型窗口，请求被网关拒绝。
        其余参数留空表示不设置、沿用引擎默认。
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        计算方式：有效窗口 = min(压缩窗口, 模型真实窗口) − min(最大输出, 20000)（未填最大输出按 20000）；
        触发阈值 = 有效窗口 − 13000，填了触发百分比时取 min(有效窗口 × 触发百分比, 有效窗口 − 13000)。
        需满足 触发阈值 + 最大输出 + 余量 ≤ 模型真实窗口。余量用于容纳单轮较大的工具结果，
        建议不少于真实窗口的 10%；不填压缩窗口和触发百分比且最大输出不超过 20000 时，余量固定为 13000，通常偏小。
        例：窗口 1000000，压缩窗口 800000，最大输出 16000，有效窗口 784000，阈值 771000，余量 213000。
      </Typography.Paragraph>
      <Form form={form} layout="vertical" onFinish={save}>
        {FIELDS.map((field) => (
          <Form.Item
            key={field.name}
            label={field.label}
            name={field.name}
            extra={`${field.help}（${field.env}）`}
          >
            <InputNumber
              min={field.min}
              max={field.max}
              precision={0}
              placeholder={defaults[field.name] ? `留空按 ${defaults[field.name]}` : field.placeholder}
              style={{ width: '100%' }}
            />
          </Form.Item>
        ))}
        <div style={{ marginBottom: 16 }}>
          <Estimate values={values} defaultWindow={defaults.max_context_tokens} />
        </div>
        <Button type="primary" htmlType="submit" loading={saving}>
          保存
        </Button>
      </Form>
    </>
  );
}
