import { Alert, Form, Input, InputNumber, Modal, Select, Typography } from 'antd';
import { useState } from 'react';
import {
  VERIFIED_AT_PATTERN,
  entryFormValues,
  entryFromForm,
  isSourceUrl,
  nameOwner,
  normalizeModelName,
  splitLines,
} from '../lib/contextPresets.js';
import { CONTEXT_LIMITS } from '../lib/contextSettings.js';

const TITLES = { add: '新增模型', edit: '编辑自定义条目', override: '覆盖内置条目' };

const tokenInput = (name) => (
  <InputNumber {...CONTEXT_LIMITS[name]} precision={0} style={{ width: '100%' }} />
);

// 推荐表条目表单。editing 为 { mode: 'add' | 'edit' | 'override', entry }，关闭时保留，避免关闭动画中内容变空；
// 调用方每次打开都须换 key 重新挂载，初始值只在表单实例首次挂载时生效。
// onSave(entry, verifiedAt) 由调用方处理保存结果与关闭
export default function ContextPresetForm({ open, editing, presets, verifiedAt, onSave, onCancel }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const mode = editing?.mode;
  // 编辑或覆盖时 id 不可改，保存后整条替换同 id 的条目，重名检查不算这一条
  const replacedId = mode === 'add' ? null : editing?.entry?.id;

  const checkId = (_, value) => {
    const id = (value || '').trim();
    if (!normalizeModelName(id)) return Promise.reject(new Error('请填写模型名'));
    const owner = mode === 'add' ? nameOwner(id, presets, null) : null;
    if (!owner) return Promise.resolve();
    return Promise.reject(
      new Error(
        normalizeModelName(owner.id) === normalizeModelName(id)
          ? `推荐表中已有 ${owner.id}，请在表中编辑或覆盖`
          : `与 ${owner.id} 的别名重名`,
      ),
    );
  };

  const checkAliases = (_, aliases) => {
    for (const alias of aliases || []) {
      if (!normalizeModelName(alias)) return Promise.reject(new Error(`别名「${alias}」无效`));
      const owner = nameOwner(alias, presets, replacedId);
      if (owner) return Promise.reject(new Error(`别名「${alias}」与 ${owner.id} 的模型名或别名重名`));
    }
    return Promise.resolve();
  };

  const checkSources = (_, text) => {
    const invalid = splitLines(text).find((url) => !isSourceUrl(url));
    return invalid
      ? Promise.reject(new Error(`「${invalid}」不是 https:// 开头的链接，其它说明请写在说明里`))
      : Promise.resolve();
  };

  const submit = async () => {
    let values;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      await onSave(entryFromForm(values), (values.verified_at || '').trim());
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={mode ? TITLES[mode] : ''}
      open={open}
      onOk={submit}
      onCancel={onCancel}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      width={560}
      destroyOnHidden
    >
      {mode === 'override' && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="保存后以这里的值替换内置条目；此后升级带来的内置修正不再作用于该模型，可随时「恢复内置」"
        />
      )}
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        改表只影响推荐与预填，已保存的上下文参数不会跟着变，需要时在配置页修改。数值请以模型厂商公布的为准。
      </Typography.Paragraph>
      <Form
        form={form}
        layout="vertical"
        initialValues={{ ...entryFormValues(editing?.entry), verified_at: verifiedAt || '' }}
      >
        <Form.Item
          label="模型名"
          name="id"
          rules={[{ validator: checkId }]}
          extra="与配置页「模型」里填的一致；匹配时忽略大小写、provider/ 前缀、「.」「_」写法和日期后缀"
        >
          <Input disabled={mode !== 'add'} placeholder="如 example-model-pro" />
        </Form.Item>
        <Form.Item label="厂商（选填）" name="vendor">
          <Input />
        </Form.Item>
        <Form.Item
          label="别名（选填）"
          name="aliases"
          rules={[{ validator: checkAliases }]}
          extra="同一模型在网关上的其它名字，输入后回车"
        >
          <Select mode="tags" open={false} tokenSeparators={[',', '，']} suffixIcon={null} />
        </Form.Item>
        <Form.Item
          label="上下文窗口"
          name="max_context_tokens"
          rules={[{ required: true, message: '请填写上下文窗口' }]}
          extra="输入 + 输出总量（tokens）"
        >
          {tokenInput('max_context_tokens')}
        </Form.Item>
        <Form.Item
          label="最大输出"
          name="max_output_tokens"
          dependencies={['max_context_tokens']}
          extra="取官方默认值；思考与非思考默认值不同时取思考模式的"
          rules={[
            { required: true, message: '请填写最大输出' },
            ({ getFieldValue }) => ({
              validator: (_, value) => {
                const contextWindow = getFieldValue('max_context_tokens');
                return value && contextWindow && value >= contextWindow
                  ? Promise.reject(new Error('最大输出必须小于上下文窗口'))
                  : Promise.resolve();
              },
            }),
          ]}
        >
          {tokenInput('max_output_tokens')}
        </Form.Item>
        <Form.Item label="说明（选填）" name="note">
          <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} />
        </Form.Item>
        <Form.Item
          label="来源链接（选填）"
          name="sources"
          rules={[{ validator: checkSources }]}
          extra="每行一个，建议填厂商官方文档"
        >
          <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} placeholder="https://example.com/docs/models" />
        </Form.Item>
        <Form.Item
          label="核对日期（选填）"
          name="verified_at"
          rules={[{ pattern: VERIFIED_AT_PATTERN, message: '请按 年-月-日 填写，如 2026-10-15' }]}
          extra="整份自定义表的核对日期：按厂商官方文档核对过数值后填写，保存时不会自动更新"
        >
          <Input placeholder="如 2026-10-15" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
