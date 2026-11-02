// 用户、审计与回收站（运维中心，设计稿 ops-users）
// 页签：用户管理 AD-01～AD-05、审计日志 O-48/O-49、错误留档 O-50/O-51、回收站 O-52～O-54。
// 角色：用户管理写操作只有 admin（ops 只读）；回收站 ops 和 admin 都能操作；审计、错误两者都能看。
// 地址参数：?tab=users|audit|errors|trash；?error=<错误编号> 直接打开原始错误抽屉（chat-states 第 15 项的错误编号）。
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Tabs } from '../../ui/index.jsx';
import { OpsShell } from './parts.jsx';
import { AccountsPane } from './usersAccounts.jsx';
import { AuditPane, ErrorDrawer, ErrorsPane } from './usersAudit.jsx';
import { mergePage } from './usersModel.js';
import { TrashPane } from './usersTrash.jsx';
import '../../styles/ops-users.css';

const TAB_OF = { users: 'u', audit: 'a', errors: 'e', trash: 't' };
const USERS_PAGE = 100;
const ERRORS_PAGE = 10;

function Body({ route, perm }) {
  const { api, user } = useApp();
  const query = route?.query || {};
  const [tab, setTab] = useState(TAB_OF[query.tab] || (query.error ? 'e' : 'u'));
  const [users, setUsers] = useState(null);
  const [usersErr, setUsersErr] = useState('');
  const [errors, setErrors] = useState(null);
  const [errorsErr, setErrorsErr] = useState('');
  const [trash, setTrash] = useState(null);
  const [trashErr, setTrashErr] = useState('');
  const [busy, setBusy] = useState('');
  const [errorRow, setErrorRow] = useState(query.error ? { id: query.error } : null);

  useEffect(() => {
    if (TAB_OF[query.tab]) setTab(TAB_OF[query.tab]);
    if (query.error) { setTab('e'); setErrorRow({ id: query.error }); }
  }, [query.tab, query.error]);

  const loadUsers = useCallback(async () => {
    try { setUsers(await api.adminUsers({ limit: USERS_PAGE })); setUsersErr(''); } catch (e) { setUsersErr(errorText(e)); }
  }, [api]);
  const loadErrors = useCallback(async () => {
    try { setErrors(mergePage(null, await api.ops.errors({ limit: ERRORS_PAGE }), { append: false })); setErrorsErr(''); } catch (e) { setErrorsErr(errorText(e)); }
  }, [api]);
  const loadTrash = useCallback(async () => {
    try { setTrash(await api.ops.trash()); setTrashErr(''); } catch (e) { setTrashErr(errorText(e)); }
  }, [api]);

  useEffect(() => { loadUsers(); loadErrors(); loadTrash(); }, [loadUsers, loadErrors, loadTrash]);

  async function moreUsers() {
    setBusy('users');
    try {
      const page = await api.adminUsers({ limit: USERS_PAGE, cursor: users.next_cursor });
      setUsers((cur) => ({ ...cur, ...mergePage(cur, page, { append: true }) }));
    } catch (e) { setUsersErr(errorText(e)); } finally { setBusy(''); }
  }
  async function moreErrors() {
    setBusy('errors');
    try {
      const page = await api.ops.errors({ limit: ERRORS_PAGE, cursor: errors.next_cursor });
      setErrors((cur) => mergePage(cur, page, { append: true }));
    } catch (e) { setErrorsErr(errorText(e)); } finally { setBusy(''); }
  }

  // 用户名 → 显示名（审计操作者、回收站所有者）
  const nameOf = useMemo(() => {
    const map = new Map((users?.items || []).map((u) => [u.username, u.display_name || u.username]));
    return (name) => map.get(name) || name;
  }, [users]);

  const usersCount = users && users.available !== false ? users.total ?? users.items?.length : null;
  return (
    <>
      <PageHead eyebrow="运维中心" title="用户、审计与回收站" desc="账号与角色、全量审计、原始错误、被删除的项目。" />
      <Tabs
        value={tab}
        onChange={setTab}
        items={[
          ['u', '用户管理', usersCount ?? null, 'users'],
          ['a', '审计日志', null, 'list'],
          ['e', '错误留档', errors?.total ?? null, 'alert'],
          ['t', '回收站', trash ? trash.items.length : null, 'trash'],
        ]}
      />
      {tab === 'u' ? (
        <AccountsPane
          data={users}
          loadError={usersErr}
          perm={perm}
          me={user?.username}
          nameOf={nameOf}
          retentionDays={trash?.retention_days}
          moreBusy={busy === 'users'}
          onMore={moreUsers}
          onReload={(o) => { loadUsers(); if (o?.trash) loadTrash(); }}
        />
      ) : null}
      {tab === 'a' ? <AuditPane nameOf={nameOf} /> : null}
      {tab === 'e' ? <ErrorsPane data={errors} loadError={errorsErr} busy={busy === 'errors'} onMore={moreErrors} onOpen={setErrorRow} /> : null}
      {tab === 't' ? <TrashPane data={trash} loadError={trashErr} perm={perm} nameOf={nameOf} onChanged={loadTrash} /> : null}
      {errorRow ? <ErrorDrawer row={errorRow} onClose={() => setErrorRow(null)} /> : null}
    </>
  );
}

export default function Page({ route }) {
  return (
    <OpsShell active="ops-users">
      {(perm) => <Body route={route} perm={perm} />}
    </OpsShell>
  );
}
