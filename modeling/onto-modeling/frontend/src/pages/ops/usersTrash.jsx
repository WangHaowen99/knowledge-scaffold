// 回收站（O-52～O-54）：ops 和 admin 都能恢复、永久删除（契约 8.1 #28）。
// B2 后端只剩旧版软删除的存量：retention_days、purge_at 为 null，不自动清除。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { Button, Empty, Modal, Note, Table } from '../../ui/index.jsx';
import { dateTimeText, purgeText, restoreDoneText, trashEmptyText, trashIntro } from './usersModel.js';

export function TrashPane({ data, loadError, perm, nameOf, onChanged }) {
  const { api } = useApp();
  const [purge, setPurge] = useState(null);
  const [busy, setBusy] = useState('');
  const items = data?.items || [];
  const write = perm.trashWrite;
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
      <div className="row mb12 ou-legend">
        <span className="muted sm">{trashIntro(data?.retention_days)}</span>
        <span className="sp" />
      </div>
      {loadError ? <Note tone="red">{loadError}</Note> : null}
      {data && !items.length ? <Empty icon="trash" title="回收站为空">{trashEmptyText(data.retention_days)}</Empty> : null}
      {items.length ? (
        <Table
          compact
          cols={['项目', '所有者', '删除时间', '大小', '自动清除', '操作']}
          rows={items.map((t) => [
            <b key="n">{t.project?.name || t.entry}</b>,
            t.project?.owner ? nameOf(t.project.owner) : '—',
            <span key="d" className="muted">{dateTimeText(t.deleted_at)}</span>,
            <span key="s" className="num">{t.size != null ? formatBytes(t.size) : '—'}</span>,
            <span key="p" className="muted">{purgeText(t.purge_at)}</span>,
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
      >
        <Note tone="gray">删除分两段：先移走，再清除；进程中途重启会继续清扫，不会留下残留。</Note>
      </Modal>
    </div>
  );
}
