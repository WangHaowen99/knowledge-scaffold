// 回收站（O-52～O-54）：ops 和 admin 都能恢复、永久删除（契约 8.1 #28）。
// B2 后端只剩旧版软删除的存量：retention_days、purge_at 为 null，不自动清除（不出「自动清除」列，清空后页签不出，R21）。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { Button, Empty, Modal, Note, Table } from '../../ui/index.jsx';
import { dateTimeText, purgeText, restoreDoneText, trashView } from './usersModel.js';

export function TrashPane({ data, loadError, perm, nameOf, onChanged }) {
  const { api } = useApp();
  const [purge, setPurge] = useState(null);
  const [busy, setBusy] = useState('');
  const items = data?.items || [];
  const write = perm.trashWrite;
  const { autoPurge } = trashView(data);
  const hint = write ? undefined : perm.hint('trash.write');

  async function restore(item) {
    setBusy(item.entry);
    try {
      const r = await api.ops.restoreTrash(item.entry);
      toast(restoreDoneText(item, r));
      onChanged();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy('');
    }
  }

  async function doPurge() {
    const item = purge;
    setBusy(item.entry);
    try {
      await api.ops.purgeTrash(item.entry);
      toast('已永久删除');
      setPurge(null);
      onChanged();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="mt16">
      {loadError ? <Note tone="red">{loadError}</Note> : null}
      {data && !items.length ? <Empty icon="trash" title="回收站为空" /> : null}
      {items.length ? (
        // 没有保留期（只剩旧版存量，R21）时不出「自动清除」列
        <Table
          compact
          cols={['项目', '所有者', '删除时间', '大小', ...(autoPurge ? ['自动清除'] : []), '操作']}
          rows={items.map((t) => [
            <b key="n">{t.project?.name || t.entry}</b>,
            t.project?.owner ? nameOf(t.project.owner) : '—',
            <span key="d" className="muted">{dateTimeText(t.deleted_at)}</span>,
            <span key="s" className="num">{t.size != null ? formatBytes(t.size) : '—'}</span>,
            ...(autoPurge ? [<span key="p" className="muted">{purgeText(t.purge_at)}</span>] : []),
            <div key="a" className="row g4">
              <Button size="xs" kind="soft" disabled={!write || busy === t.entry} title={hint} onClick={() => restore(t)}>恢复</Button>
              <Button size="xs" kind="danger" disabled={!write || busy === t.entry} title={hint} onClick={() => setPurge(t)}>永久删除</Button>
            </div>,
          ])}
        />
      ) : null}
      <Modal
        open={!!purge}
        onClose={() => setPurge(null)}
        danger
        icon="trash"
        title={`永久删除「${purge?.project?.name || purge?.entry || ''}」？`}
        sub="工作区、对话记录与版本历史都会被清除，不可恢复"
        foot={<><Button onClick={() => setPurge(null)}>取消</Button><Button kind="danger" className="fill" disabled={!!busy} onClick={doPurge}>永久删除</Button></>}
      />
    </div>
  );
}
