import { Alert, Form, InputNumber, Typography } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { CONTEXT_LIMITS, defaultValuesOf, estimateCompaction } from '../lib/contextSettings.js';
import ContextPresetsModal from './ContextPresetsModal.jsx';

const WINDOW_FIELD = {
  name: 'max_context_tokens',
  label: '上下文窗口',
  ...CONTEXT_LIMITS.max_context_tokens,
  placeholder: '如 200000',
  help: '模型可接受的输入 + 输出总量（tokens），以模型厂商公布的值为准。',
};

const OUTPUT_FIELD = {
  name: 'max_output_tokens',
  label: '最大输出',
  ...CONTEXT_LIMITS.max_output_tokens,
  placeholder: '如 16000',
  help: '每次回答最多生成的 tokens；越大留给对话的空间越少，过小会截断回答。',
};

function ContextField({ field, fallback }) {
  return (
    <Form.Item label={field.label} name={field.name} extra={field.help}>
      <InputNumber
        min={field.min}
        max={field.max}
        precision={0}
        placeholder={fallback ? `留空按 ${fallback.toLocaleString()}` : field.placeholder}
        style={{ width: '100%' }}
      />
    </Form.Item>
  );
}

// 按输入框里的值实时估算（压缩参数由系统推算，配置页不展示）
function CompactionSummary({ estimate }) {
  if (!estimate) return null;
  if (estimate.error) return <Alert type="warning" showIcon message={estimate.error} />;
  const text = `根据当前配置，上下文长度达到约 ${estimate.compactAt.toLocaleString()} tokens（${estimate.percent}%）时自动压缩`;
  if (estimate.margin !== null && estimate.margin <= 0) {
    return (
      <Alert type="warning" showIcon message={`${text}；最大输出过大，压缩前的请求可能超出上下文窗口，请调小最大输出`} />
    );
  }
  return <Alert type="info" showIcon message={text} />;
}

// form 由模型接入页传入：那边按模型名查推荐表预填，并与网关配置共用一个保存按钮；
// 推荐表在弹窗里改过后调用 onPresetsChanged，由那边重新查表
export default function ModelContextSettings({ form, current, onPresetsChanged }) {
  const [presetsOpen, setPresetsOpen] = useState(false);
  const closePresets = useCallback(() => setPresetsOpen(false), []);
  // 每项：{ value, source, env }，value 为该项留空时实际生效的值
  const defaults = current?.context_defaults || {};
  const savedKey = JSON.stringify(current?.context ?? null);
  const estimate = estimateCompaction(
    {
      [WINDOW_FIELD.name]: Form.useWatch(WINDOW_FIELD.name, form),
      [OUTPUT_FIELD.name]: Form.useWatch(OUTPUT_FIELD.name, form),
    },
    defaultValuesOf(current?.context_defaults),
  );

  // 只在已保存的参数变化时回填：保存模型、内测引擎等其他配置不应冲掉推荐表预填或手改的值
  useEffect(() => {
    const saved = JSON.parse(savedKey);
    if (saved) form.setFieldsValue(saved);
  }, [savedKey, form]);

  return (
    <>
      <Typography.Title level={5}>上下文与自动压缩（稳定引擎）</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: -4 }}>
        填写模型名后，命中推荐表会自动填入上下文窗口和最大输出，可修改；未命中时清空，需按厂商公布的值填写，也可在推荐表中新增该模型。自动压缩的时机由系统按这两项推算。
        <Typography.Link onClick={() => setPresetsOpen(true)}> 查看或编辑推荐表</Typography.Link>
      </Typography.Paragraph>
      <ContextPresetsModal open={presetsOpen} onClose={closePresets} onChanged={onPresetsChanged} />
      <Form form={form} layout="vertical">
        <ContextField field={WINDOW_FIELD} fallback={defaults[WINDOW_FIELD.name]?.value} />
        <ContextField field={OUTPUT_FIELD} fallback={defaults[OUTPUT_FIELD.name]?.value} />
        {estimate && (
          <div style={{ marginBottom: 16 }}>
            <CompactionSummary estimate={estimate} />
          </div>
        )}
      </Form>
    </>
  );
}
