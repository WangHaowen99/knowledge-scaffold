// 运维管控与健康的小件：显示名表、状态 chip、带原因的确认弹窗、带进度的操作封装
import { useCallback, useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Area, Button, Chip, Field, Modal, Note, St } from '../../ui/index.jsx';

// 用户名 → 显示名：AD-01（ops、admin 都能读）；拿不到时页面显示用户名
export function useUserNames() {
  const { api } = useApp();
  const [names, setNames] = useState({});
  useEffect(() => {
    let alive = true;
    api.adminUsers({ limit: 200 }).then((r) => {
      if (!alive) return;
      setNames(Object.fromEntries((r.items || []).map((u) => [u.username, u.display_name || u.username])));
    }).catch(() => {});
    return () => { alive = false; };
  }, [api]);
  return names;
}

// 页面读完一轮后仍为 null 的块：接口没响应或还没开放（B2、B3b 有几组运维接口没做，见 gaps）
export function Missing() {
  return <div className="xs muted mt8">暂时读不到这一块（接口没有响应或还没开放）</div>;
}

// ctlModel 里 { label, tone, icon, dot } 形状的 chip；st=running 用转圈的运行中
export function ViewChip({ v, sm = true }) {
  if (!v) return null;
  if (v.st) return <St k={v.st} sm={sm} label={v.label} />;
  return <Chip tone={v.tone} icon={v.icon || undefined} dot={v.dot} sm={sm}>{v.label}</Chip>;
}

// 跑一个写操作：成功 toast，失败把错误说成人话；返回是否成功
export function useAct() {
  const [busy, setBusy] = useState(null);
  const run = useCallback(async (key, fn, { ok, icon = 'check', errText } = {}) => {
    setBusy(key);
    try {
      const r = await fn();
      if (ok) toast(typeof ok === 'function' ? ok(r) : ok, icon);
      return r ?? true;
    } catch (e) {
      toast((errText && errText(e)) || errorText(e), 'alert');
      return null;
    } finally {
      setBusy(null);
    }
  }, []);
  return [busy, run];
}

/**
 * 确认弹窗：设计稿 opKill 那种（提示条 + 可选的原因输入）。
 * reason: 'required' | 'optional' | null；onConfirm(reason) 返回 Promise，成功时关闭。
 */
export function ConfirmModal({
  open, onClose, icon, danger, title, sub, note, noteTone = 'amber', noteIcon, children, reason = null,
  reasonLabel = '原因（写进审计）', placeholder = '例如：维护窗口前排空', okText, onConfirm, w,
}) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setText(''); setBusy(false); } }, [open]);
  const blocked = reason === 'required' && !text.trim();
  const go = async () => {
    setBusy(true);
    const ok = await onConfirm(text.trim());
    setBusy(false);
    if (ok) onClose();
  };
  return (
    <Modal
      open={open} onClose={onClose} icon={icon} danger={danger} title={title} sub={sub} w={w}
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind={danger ? 'danger' : 'primary'} className={danger ? 'fill' : undefined} disabled={blocked || busy} onClick={go}>{okText}</Button>
        </>
      )}
    >
      {note ? <Note tone={noteTone} icon={noteIcon}>{note}</Note> : null}
      {children}
      {reason ? (
        <div className="mt12">
          <Field label={reasonLabel}>
            <Area minHeight={60} textareaStyle={{ height: 40 }} placeholder={placeholder} value={text} onChange={(e) => setText(e.target.value)} />
          </Field>
        </div>
      ) : null}
    </Modal>
  );
}
