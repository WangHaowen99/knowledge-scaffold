// 用户管理分区（AD-01～AD-05）：账号表、改角色菜单、统计卡、「用户管理不可用」，以及重置密码、删除用户两个弹窗。
// 写操作只有 admin；ops 只读（按钮停用并说明，版式不变）。
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText, formatBytes } from '../../api/errors.js';
import {
  Avatar, Button, Chip, Empty, Field, Icon, Inp, Menu, Modal, Note, Select, Stat, Table, cx, useOutside,
} from '../../ui/index.jsx';
import {
  PASSWORD_HINT, ROLE_TONE, avatarIndex, defaultTransferTarget, deleteDoneText, deleteRequest, deleteSub, isTrashUnsupported,
  lastSeenText, resetRequest, roleLabel, roleMenu, ruleTexts, transferCandidates, trashOption, userOptionLabel, userStats,
} from './usersModel.js';

const SELF_HINT = '不能修改自己的账号';

function RoleButton({ u, disabled, title, onPick }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  return (
    <div ref={ref} className="row g4" style={{ position: 'relative' }}>
      <Button size="xs" className={open ? 'soft' : undefined} disabled={disabled} title={title} after={<Icon name="chevd" size={12} />} onClick={() => setOpen((v) => !v)}>
        改角色
      </Button>
      {open ? (
        <div style={{ position: 'absolute', top: 30, left: 0, zIndex: 5 }}>
          <Menu
            style={{ minWidth: 170 }}
            items={roleMenu(u.role).map((r) => ({ label: r.label, icon: r.icon, hint: r.hint, value: r.value, title: r.title }))}
            onPick={(x) => { setOpen(false); if (x.value !== u.role) onPick(x.value); }}
          />
        </div>
      ) : null}
    </div>
  );
}

export function AccountsPane({ data, loadError, perm, me, nameOf, retentionDays, onReload, onMore, moreBusy }) {
  const { api } = useApp();
  const [pwdFor, setPwdFor] = useState(null);
  const [delFor, setDelFor] = useState(null);
  const unavailable = data?.available === false;
  const items = data?.items || [];
  const write = perm.usersWrite;
  const writeHint = write ? undefined : perm.hint('users.write');

  async function changeRole(u, role) {
    try {
      await api.setUserRole(u.username, role);
      toast(`已把 ${u.display_name || u.username} 改为${roleLabel(role)}`);
      onReload();
    } catch (e) {
      toast(e?.details?.reason === 'last_admin' ? '不能降级最后一个管理员' : errorText(e), 'alert');
    }
  }

  const rows = items.map((u) => {
    const self = u.username === me;
    const locked = !write || self;
    const hint = !write ? writeHint : self ? SELF_HINT : undefined;
    return [
      <div className="row" key="u">
        <Avatar text={(u.display_name || u.username).slice(0, 1)} size="sm" i={avatarIndex(u.username)} />
        <div><div className="b">{u.display_name || u.username}</div><div className="xs faint mono">{u.username}</div></div>
      </div>,
      <Chip key="r" tone={ROLE_TONE[u.role]} sm>{roleLabel(u.role)}</Chip>,
      u.online ? <Chip key="s" tone="teal" dot sm>在线</Chip> : <Chip key="s" tone="gray" sm>离线</Chip>,
      <span key="t" className="muted">{lastSeenText(u.last_seen_at)}</span>,
      <span key="n" className="num">{u.project_count ?? '—'}</span>,
      <div key="a" className="row g4">
        <RoleButton u={u} disabled={locked} title={hint} onPick={(role) => changeRole(u, role)} />
        <Button size="xs" disabled={!write} title={writeHint} onClick={() => setPwdFor(u)}>重置密码</Button>
        <Button size="xs" kind="danger" disabled={locked} title={hint} onClick={() => setDelFor(u)}>删除</Button>
      </div>,
    ];
  });

  return (
    <div className="mt16">
      {loadError ? <Note tone="red">{loadError}</Note> : null}
      {unavailable ? (
        <>
          <Note tone="red" icon="key">
            <b>用户管理不可用：</b>服务端未配置认证服务管理令牌（<span className="mono">ONTO_WEB_AUTH_ADMIN_TOKEN</span>、<span className="mono">AUTH_SERVER_ADMIN_TOKEN</span>）
          </Note>
          <Empty icon="users" title="暂时无法列出账号" />
        </>
      ) : data ? (
        <>
          <Table cols={['用户', '角色', '状态', '最近活跃', '项目数', '操作']} rows={rows} />
          {data.next_cursor ? (
            <div className="xs muted mt8">
              显示前 {items.length} 个，共 {data.total ?? '—'} 个 · <Button size="xs" kind="ghost" disabled={moreBusy} onClick={onMore}>加载更多</Button>
            </div>
          ) : null}
          <div className="grid4 mt16">
            {userStats(data.stats, data.total).map((s) => <Stat key={s.key} label={s.label} value={s.value} icon={s.icon} desc={s.desc} />)}
          </div>
        </>
      ) : null}
      {pwdFor ? <ResetPasswordModal target={pwdFor} onClose={() => setPwdFor(null)} /> : null}
      {delFor ? (
        <DeleteUserModal
          target={delFor}
          users={items}
          nameOf={nameOf}
          retentionDays={retentionDays}
          onClose={() => setDelFor(null)}
          onDone={() => { setDelFor(null); onReload({ trash: true }); }}
        />
      ) : null}
    </div>
  );
}

// ---------- 重置密码（AD-03） ----------

export function ResetPasswordModal({ target, onClose }) {
  const { api } = useApp();
  const [pw, setPw] = useState('');
  const [generate, setGenerate] = useState(false);
  const [problem, setProblem] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState('');
  const display = target.display_name || target.username;

  useEffect(() => { setProblem(''); }, [pw, generate]);

  async function submit() {
    const req = resetRequest({ username: target.username, password: pw, generate });
    if (!req.ok) { setProblem(req.error); return; }
    setBusy(true);
    try {
      const r = await api.resetUserPassword(target.username, req.body);
      if (r?.generated_password) setResult(r.generated_password);
      else { toast('密码已重置'); onClose(); }
    } catch (e) {
      if (e?.code === 'weak_password') setProblem(`不满足：${ruleTexts(e.details?.failed_rules).join('、')}`);
      else toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    try { await navigator.clipboard.writeText(result); toast('已复制'); } catch { toast('复制失败，请手动选中复制', 'alert'); }
  }

  return (
    <Modal
      open
      onClose={onClose}
      icon="key"
      title="重置密码"
      sub={`${display} · ${target.username}`}
      sticky={!!result}
      foot={result
        ? <Button kind="primary" onClick={onClose}>我已保存，关闭</Button>
        : <><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={busy} onClick={submit}>重置</Button></>}
    >
      {result ? (
        <div className="col" style={{ gap: 12 }}>
          <Note tone="teal" icon="check">密码只显示这一次，请转交给{display}。</Note>
          <Field label="新密码">
            <Inp value={result} readOnly mono after={<Button size="xs" kind="ghost" icon="copy" onClick={copy}>复制</Button>} />
          </Field>
        </div>
      ) : (
        <div className="col" style={{ gap: 12 }}>
          <Field label="新密码" hint={problem ? <span style={{ color: 'var(--red)' }}>{problem}</span> : PASSWORD_HINT}>
            <Inp
              type="password"
              autoComplete="new-password"
              value={generate ? '' : pw}
              disabled={generate}
              error={!!problem}
              placeholder={generate ? '自动生成' : '输入新密码'}
              onChange={(e) => setPw(e.target.value)}
              after={<Button size="xs" kind="ghost" onClick={() => setGenerate((v) => !v)}>{generate ? '手动输入' : '生成'}</Button>}
            />
          </Field>
        </div>
      )}
    </Modal>
  );
}

// ---------- 删除用户（AD-04、AD-05） ----------

export function DeleteUserModal({ target, users, nameOf, retentionDays, onClose, onDone }) {
  const { api } = useApp();
  const [projects, setProjects] = useState(null);
  const [loadErr, setLoadErr] = useState('');
  const [mode, setMode] = useState('transfer');
  const [to, setTo] = useState(() => defaultTransferTarget(users, target.username));
  const [trashBlocked, setTrashBlocked] = useState(false);
  const [problem, setProblem] = useState('');
  const [busy, setBusy] = useState(false);
  const display = target.display_name || target.username;
  const opt = trashBlocked ? trashOption(null) : trashOption(retentionDays);
  const candidates = transferCandidates(users, target.username);

  useEffect(() => {
    let alive = true;
    api.userProjects(target.username)
      .then((r) => alive && setProjects(r.items || []))
      .catch((e) => alive && (setProjects([]), setLoadErr(errorText(e))));
    return () => { alive = false; };
  }, [api, target.username]);
  useEffect(() => { setProblem(''); }, [mode, to]);

  async function submit() {
    const req = deleteRequest({ projectCount: projects?.length || 0, mode, target: to, users, username: target.username, trashEnabled: opt.enabled });
    if (!req.ok) { setProblem(req.error); return; }
    setBusy(true);
    try {
      const r = await api.deleteUser(target.username, req.body);
      toast(deleteDoneText({ ...r, transfer_to: to }, nameOf));
      onDone();
    } catch (e) {
      // 后端说回收站不可用：退回只能移交的版式（R20），不另加说明
      if (isTrashUnsupported(e)) { setTrashBlocked(true); setMode('transfer'); }
      else setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const count = projects ? projects.length : null;
  const transferPicker = candidates.length
    ? <Select value={to} options={candidates.map((u) => ({ value: u.username, label: userOptionLabel(u) }))} onChange={setTo} />
    : <div className="xs muted">没有其他建模者可以接手</div>;
  return (
    <Modal
      open
      onClose={onClose}
      danger
      icon="trash"
      w={580}
      title={`删除用户 ${display}？`}
      sub={deleteSub(target, count)}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="danger" className="fill" disabled={busy || projects == null} onClick={submit}>删除用户</Button></>}
    >
      {projects == null ? <div className="muted sm">正在读取名下的项目…</div> : null}
      {loadErr ? <Note tone="red">{loadErr}</Note> : null}
      {projects && projects.length ? (
        <>
          <Table compact cols={['项目', '阶段', '大小']} rows={projects.map((p) => [p.name, p.stage_label || '—', p.disk_bytes != null ? formatBytes(p.disk_bytes) : '—'])} />
          {opt.enabled ? (
            <div className="mt12 col" style={{ gap: 8 }}>
              <div className="b sm">这些项目怎么处理</div>
              <div className={cx('opt', mode === 'transfer' && 'on')} onClick={() => setMode('transfer')}>
                <span className="k">1</span>
                <div className="grow">
                  <div className="t">移交给另一位建模者</div>
                  <div className="mt8 ou-sel" style={{ width: 240 }}>{transferPicker}</div>
                </div>
              </div>
              <div className={cx('opt', mode === 'trash' && 'on')} onClick={() => setMode('trash')}>
                <span className="k">2</span>
                <div className="grow">
                  <div className="t">移入回收站</div>
                  <div className="c">{opt.text}</div>
                </div>
              </div>
            </div>
          ) : (
            // 回收站不可用（R20）：不出二选一，项目表下面直接一行「移交给」
            <div className="row g12 mt12">
              <span className="b sm">移交给</span>
              <div className="ou-sel" style={{ width: 240 }}>{transferPicker}</div>
            </div>
          )}
        </>
      ) : null}
      {projects && !projects.length && !loadErr ? (
        <Note tone="gray">MCP token 和个人模型配置一并删除，不能恢复。</Note>
      ) : null}
      {problem ? <div className="xs mt8" style={{ color: 'var(--red)' }}>{problem}</div> : null}
    </Modal>
  );
}
