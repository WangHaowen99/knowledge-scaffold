// 知识包共享（develop !590 成员向导的 Hub 同步部分，ADR-0098；HB-11 / HB-12）：所有者加共享用户同步成 Hub 授权用户。
// 项目只有所有者，共享在知识包这一层管理、不碰项目权限。按用户名添加；保存后已发布的包立即同步，
// 失败的用户旁边看失败原因、「重试授权 / 重试撤权」。入口在发布页「已发布版本」表头：「共享给 N 人」。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Card, Chip, Icon, Inp, Modal, Note } from '../../ui/index.jsx';
import {
  SAVE_FAILED_WARNING, SHARING_EMPTY, SHARING_NOTE, addProblem, pendingRevokes, syncFailed, userSync,
} from './sharingModel.js';

function SyncTools({ sync, user, retrying, onRetry }) {
  if (!sync) return null;
  return (
    <>
      <span className="row g4" style={{ color: 'var(--red)' }} title={sync.reason} data-pop={sync.reason} aria-label="查看授权失败原因">
        <Icon name="alert" size={14} />
      </span>
      <Button size="xs" disabled={!!retrying && retrying !== user} onClick={() => onRetry(user)}>
        {retrying === user ? '正在重试…' : sync.retry}
      </Button>
    </>
  );
}

function SharingModal({ projectId, onClose, onChanged }) {
  const { api } = useApp();
  const [state, setState] = useState(null);
  const [users, setUsers] = useState([]);
  const [input, setInput] = useState('');
  const [saving, setSaving] = useState(false);
  const [retrying, setRetrying] = useState(null);

  useEffect(() => {
    let alive = true;
    api.hubSharing(projectId)
      .then((r) => { if (alive) { setState(r); setUsers(r.users || []); } })
      .catch((e) => { toast(errorText(e), 'alert'); onClose(); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只在打开时读一次，onClose 每次渲染都是新函数
  }, [api, projectId]);
  if (!state) return null;
  const problem = input ? addProblem(input, users, state.owner) : '';
  const add = () => {
    if (addProblem(input, users, state.owner)) return;
    setUsers([...users, input.trim()].sort());
    setInput('');
  };
  const save = async () => {
    setSaving(true);
    try {
      const result = await api.saveHubSharing(projectId, users);
      setState(result);
      setUsers(result.users || []);
      onChanged?.(result);
      if (syncFailed(result.hub_sync)) {
        toast(SAVE_FAILED_WARNING, 'alert');
        return;
      }
      toast('共享用户已更新', 'check');
      onClose();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setSaving(false);
    }
  };
  const retry = async (user) => {
    setRetrying(user);
    try {
      const result = await api.retryHubSharing(projectId, user);
      setState((current) => ({ ...current, hub_sync: result.hub_sync }));
      onChanged?.(result);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setRetrying(null);
    }
  };
  const revokes = pendingRevokes(state.hub_sync, users);
  return (
    <Modal open onClose={onClose} icon="users" title="知识包共享" w={560}
      foot={<><span className="sp" /><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={saving || !!retrying} onClick={save}>保存</Button></>}>
      <Note>{SHARING_NOTE}</Note>
      <div className="mt12 sm muted">所有者：{state.owner || '—'}</div>
      {!users.length ? <div className="sm muted mt12">{SHARING_EMPTY}</div> : null}
      <div className="col mt8" style={{ gap: 6 }}>
        {users.map((user) => (
          <div key={user} className="row g8">
            <span className="grow mono sm">{user}</span>
            <Chip sm tone="blue">共享用户</Chip>
            <SyncTools sync={userSync(state.hub_sync, user)} user={user} retrying={retrying} onRetry={retry} />
            <Button size="xs" kind="ghost" disabled={!!retrying} onClick={() => setUsers(users.filter((x) => x !== user))}>移除</Button>
          </div>
        ))}
        {revokes.map((user) => (
          <div key={`revoke-${user}`} className="row g8">
            <span className="grow mono sm muted">{user}</span>
            <SyncTools sync={userSync(state.hub_sync, user)} user={user} retrying={retrying} onRetry={retry} />
          </div>
        ))}
      </div>
      <div className="row g8 mt12">
        <Inp className="grow" placeholder="用户名" value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
        <Button disabled={!input || !!problem} title={problem || undefined} onClick={add}>添加</Button>
      </div>
      {problem ? <div className="xs mt4" style={{ color: 'var(--red)' }}>{problem}</div> : null}
    </Modal>
  );
}

// 发布页「已发布版本」表头：共享用户数与同步状态，点开管理
export default function KnowledgeSharing({ projectId, refreshKey }) {
  const { api } = useApp();
  const [state, setState] = useState(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let alive = true;
    api.hubSharing(projectId).then((r) => { if (alive) setState(r); }).catch(() => {});
    return () => { alive = false; };
  }, [api, projectId, refreshKey]);
  const failed = syncFailed(state?.hub_sync) || Object.keys(state?.hub_sync?.pending || {}).length > 0;
  return (
    <>
      <Button size="sm" kind="ghost" icon="users" onClick={() => setOpen(true)}>
        {state ? `共享给 ${(state.users || []).length} 人` : '共享'}
        {failed ? <span style={{ color: 'var(--red)' }}> · 授权同步失败</span> : null}
      </Button>
      {open ? <SharingModal projectId={projectId} onClose={() => setOpen(false)} onChanged={setState} /> : null}
    </>
  );
}
