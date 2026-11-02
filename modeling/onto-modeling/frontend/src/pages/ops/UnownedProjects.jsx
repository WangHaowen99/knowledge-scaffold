// 无主项目与指定所有者（AD-06、AD-07）：没有所有者的项目谁都打不开，等管理员指定后才归新所有者。
// 设计稿没画入口，暂不挂载（见 gaps.md F4）。契约 8.2 建议入口是 ops-users「用户管理」加一个「无主项目」筛选。
// 挂载方法：在 usersAccounts.jsx 的 AccountsPane 里加 `const [view, setView] = useState('all')`，角色说明那一行的
// `<span className="sp" />` 后面放 `<Seg items={[['all', '全部账号'], ['unowned', '无主项目']]} value={view} onChange={setView} />`，
// 选 unowned 时用 `<UnownedProjects users={items} />` 代替账号表与统计卡。只有 admin 能用（ops 调 AD-06 会 403），组件自己判断。
import { useCallback, useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import { can, deniedHint } from '../../state/roles.js';
import {
  Button, Chip, Empty, Field, Icon, Modal, Note, Select, Table,
} from '../../ui/index.jsx';
import { lastSeenText, ownerCandidates, ownerRequest, userOptionLabel } from './usersModel.js';

export function UnownedProjects({ users }) {
  const { api, user } = useApp();
  const admin = can(user, 'admin');
  const [data, setData] = useState(null);
  const [loadErr, setLoadErr] = useState('');
  const [pick, setPick] = useState(null);

  const load = useCallback(async () => {
    try { setData(await api.unownedProjects()); setLoadErr(''); } catch (e) { setLoadErr(errorText(e)); }
  }, [api]);
  useEffect(() => { if (admin) load(); }, [admin, load]);

  if (!admin) {
    return <Empty icon="lock" title="只有管理员能指定项目所有者">{deniedHint('admin')}。运维只能在用户管理里查看账号。</Empty>;
  }
  const items = data?.items || [];
  return (
    <div>
      {loadErr ? <Note tone="red">{loadErr}</Note> : null}
      {data && !items.length ? (
        <Empty icon="folder" title="没有无主项目">所有项目都有所有者。旧版数据迁移时没记下所有者的项目会出现在这里。</Empty>
      ) : null}
      {items.length ? (
        <>
          <Table
            cols={['项目', '阶段', '构建方式', '占用', '最近活动', '操作']}
            rows={items.map((p) => [
              <div key="n" className="row g4"><Icon name="lock" size={12} /><div><div className="b">{p.name}</div><div className="xs faint mono">{p.id}</div></div></div>,
              <span key="s" className="muted">{p.stage_label || '—'}</span>,
              <Chip key="m" tone={p.build_mode === 'L4' ? 'indigo' : 'line'} sm>{p.build_mode}</Chip>,
              <span key="d" className="num">{p.disk_bytes != null ? formatBytes(p.disk_bytes) : '—'}</span>,
              <span key="t" className="muted">{lastSeenText(p.last_activity_at)}</span>,
              <Button key="a" size="xs" kind="soft" onClick={() => setPick(p)}>指定所有者</Button>,
            ])}
          />
          <div className="xs muted mt8">没有所有者的项目谁都打不开；指定后只归新所有者，管理员和运维仍只能看元数据。每次指定都写审计。</div>
        </>
      ) : null}
      {pick ? <SetOwnerModal project={pick} users={users} onClose={() => setPick(null)} onDone={() => { setPick(null); load(); }} /> : null}
    </div>
  );
}

export function SetOwnerModal({ project, users, onClose, onDone }) {
  const { api } = useApp();
  const options = ownerCandidates(users);
  const [owner, setOwner] = useState(options[0]?.username || '');
  const [problem, setProblem] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit() {
    const req = ownerRequest(owner, users);
    if (!req.ok) { setProblem(req.error); return; }
    setBusy(true);
    try {
      await api.setProjectOwner(project.id, req.body);
      const u = options.find((x) => x.username === owner);
      toast(`已把「${project.name}」指定给${u?.display_name || owner}，已写审计`);
      onDone();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      icon="user"
      title="指定所有者"
      sub={`${project.name} · 现在没有所有者`}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={busy || !owner} onClick={submit}>指定</Button></>}
    >
      <div className="col" style={{ gap: 12 }}>
        <Field label="新所有者" hint="项目只有一个所有者；指定后对方在「我的项目」里就能打开">
          <div className="ou-sel">
            {options.length
              ? <Select value={owner} options={options.map((u) => ({ value: u.username, label: userOptionLabel(u) }))} onChange={(v) => { setOwner(v); setProblem(''); }} />
              : <div className="xs muted">读不到账号列表（认证服务没有配置管理令牌）</div>}
          </div>
        </Field>
        <Note tone="gray">指定给自己也能打开项目内容，所以每次指定都会记下原值与新值写进审计。</Note>
        {problem ? <div className="xs" style={{ color: 'var(--red)' }}>{problem}</div> : null}
      </div>
    </Modal>
  );
}
