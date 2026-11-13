import { Alert, Button, Input, Modal, Radio, Typography } from 'antd';
import { useState } from 'react';
import { limitProjectName, MAX_NAME_LENGTH } from '../lib/projectName.js';

// 任务名称同名校验（与知识包建模工具同一口径）：后端返回 409 + 候选名，这里选名后重试
export { MAX_NAME_LENGTH } from '../lib/projectName.js';
const CUSTOM = '__custom__'; // 候选名都以「（n）」结尾，不会与之相同

const normalize = (value) => String(value ?? '').trim().normalize('NFKC').toLowerCase();

// 返回不能提交的原因；空串表示可以提交
export function nameSelectionError(name, conflict) {
  const trimmed = String(name ?? '').trim();
  if (!trimmed) return '请输入新的任务名称';
  if ([...trimmed].length > MAX_NAME_LENGTH) return `任务名称不能超过 ${MAX_NAME_LENGTH} 个字符`;
  const taken = [conflict?.attempted_name, conflict?.original_name].map(normalize);
  if (taken.includes(normalize(trimmed))) return '新名称不能与原名称相同';
  return '';
}

function conflictOf(error) {
  if (error?.status !== 409 || error.code !== 'project_name_conflict') return null;
  const conflict = error.data?.conflict;
  return conflict && typeof conflict === 'object' ? conflict : null;
}

function ConflictForm({ conflict, busy, error, confirmText, busyText, onCancel, onConfirm }) {
  const candidates = conflict.candidates || [];
  const [choice, setChoice] = useState(candidates[0] ?? CUSTOM);
  const [custom, setCustom] = useState('');
  const [localError, setLocalError] = useState('');
  const selected = choice === CUSTOM ? custom : choice;

  const submit = () => {
    if (busy) return;
    const problem = nameSelectionError(selected, conflict);
    setLocalError(problem);
    if (!problem) onConfirm(selected.trim());
  };

  return (
    <>
      <Typography.Paragraph>
        已存在名为“{conflict.attempted_name}”的任务。请选择候选名称，或输入新名称。
      </Typography.Paragraph>
      <Radio.Group
        value={choice}
        disabled={busy}
        onChange={(event) => {
          setChoice(event.target.value);
          setLocalError('');
        }}
        style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
      >
        {candidates.map((candidate) => (
          <Radio key={candidate} value={candidate}>
            {candidate}
          </Radio>
        ))}
        <Radio value={CUSTOM}>自定义名称</Radio>
      </Radio.Group>
      {choice === CUSTOM && (
        <Input
          style={{ marginTop: 8 }}
          value={custom}
          placeholder="输入新的任务名称"
          autoFocus
          disabled={busy}
          onChange={(event) => {
            setCustom(limitProjectName(event.target.value));
            setLocalError('');
          }}
          onPressEnter={() => submit()}
        />
      )}
      {(localError || error) && (
        <Alert type="error" showIcon style={{ marginTop: 12 }} message={localError || error} />
      )}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
        <Button disabled={busy} onClick={() => onCancel()}>
          取消
        </Button>
        <Button type="primary" loading={busy} onClick={() => submit()}>
          {busy ? busyText : confirmText}
        </Button>
      </div>
    </>
  );
}

export default function NameConflictDialog({
  conflict,
  busy = false,
  error = '',
  confirmText = '改名并导入',
  busyText = '导入中…',
  onCancel,
  onConfirm,
}) {
  return (
    <Modal
      open={!!conflict}
      title="任务名称已存在"
      footer={null}
      width={460}
      zIndex={1100} // 叠在导入向导弹窗之上
      closable={!busy}
      maskClosable={!busy}
      keyboard={!busy}
      onCancel={() => {
        if (!busy) onCancel();
      }}
    >
      {conflict && (
        <ConflictForm
          // 换了一次冲突（重试仍重名）就重置选择
          key={`${conflict.attempted_name}|${(conflict.candidates || []).join('|')}`}
          conflict={conflict}
          busy={busy}
          error={error}
          confirmText={confirmText}
          busyText={busyText}
          onCancel={onCancel}
          onConfirm={onConfirm}
        />
      )}
    </Modal>
  );
}

/* 用法：catch 里先 offer(error, retry, 文案)，返回 false 再走原来的报错。
   retry(name) 用新名称重新提交，失败须抛出：仍重名换一组候选，其他错误显示在弹窗内。 */
export function useNameConflict() {
  const [state, setState] = useState(null); // {conflict, retry, confirmText, busyText, busy, error}

  const offer = (error, retry, texts = {}) => {
    const conflict = conflictOf(error);
    if (!conflict) return false;
    setState({ ...texts, conflict, retry, busy: false, error: '' });
    return true;
  };

  const confirm = async (name) => {
    const { retry } = state;
    setState((current) => current && { ...current, busy: true, error: '' });
    try {
      await retry(name);
      setState(null);
    } catch (error) {
      const next = conflictOf(error);
      setState((current) =>
        current && (next
          ? { ...current, conflict: next, busy: false }
          : { ...current, busy: false, error: error?.message || '操作失败，请重试' }),
      );
    }
  };

  const dialog = (
    <NameConflictDialog
      conflict={state?.conflict || null}
      busy={!!state?.busy}
      error={state?.error || ''}
      confirmText={state?.confirmText}
      busyText={state?.busyText}
      onCancel={() => setState(null)}
      onConfirm={confirm}
    />
  );
  return { offer, dialog };
}
