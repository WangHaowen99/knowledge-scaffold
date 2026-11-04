import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { api, allPages, TOKEN, REFRESH, clearLogin, saveLogin } from './api.js';
import { receiveHandoff } from './handoff.js';
import { ACTIVE, parseRoute, shortId, sourceSummary, taskRows } from './domain.js';
import { Alert, Badge, Breadcrumb, Button, Confirm, Empty, Heading, Icon, Skeleton, useAction } from './ui.jsx';
import { TaskCenter } from './TaskCenter.jsx';
import { Wizard } from './Wizard.jsx';
import { RunDetail } from './RunDetail.jsx';
import { CaseEditor } from './CaseEditor.jsx';
import { SolverCaseEditor } from './SolverCaseEditor.jsx';
import { SourceEditor } from './SourceEditor.jsx';
import { JudgeSettings } from './JudgeSettings.jsx';
import { UserMenu } from './UserMenu.jsx';

// Loaded on first use: the page only exists where the deployment enabled consumer feedback.
const FeedbackList = lazy(() => import('./FeedbackPage.jsx').then((module) => ({ default: module.FeedbackList })));
const FeedbackRun = lazy(() => import('./FeedbackPage.jsx').then((module) => ({ default: module.FeedbackRun })));

const brandIconUrl = `${import.meta.env.BASE_URL}favicon.svg`;

export default function App() {
  const [route, setRoute] = useState(() =>
      location.hash
        ? parseRoute(location.hash)
        : new URLSearchParams(location.search).get('release_id')
          ? { page: 'new', from: '' }
          : parseRoute(''),
    ),
    [session, setSession] = useState(null),
    [authLoading, setAuthLoading] = useState(true),
    [authError, setAuthError] = useState('');
  const [runs, setRuns] = useState([]),
    [sets, setSets] = useState([]),
    [solverSets, setSolverSets] = useState([]),
    [sources, setSources] = useState([]),
    [loading, setLoading] = useState(false),
    [syncError, setSyncError] = useState(''),
    [lastSync, setLastSync] = useState(null);
  const [searchState, setSearchState] = useState({ tab: 'all', search: '', packageFilter: '' }),
    [confirmation, setConfirmation] = useState(null),
    [leave, setLeave] = useState(null);
  const authEpoch = useRef(0),
    listLock = useRef(false),
    dirty = useRef(false),
    routeRef = useRef(location.hash),
    contentRef = useRef(null),
    listScroll = useRef(0);
  function reset() {
    authEpoch.current += 1;
    listLock.current = false;
    setSession(null);
    setRuns([]);
    setSets([]);
    setSolverSets([]);
    setSources([]);
    setSyncError('');
    setLastSync(null);
    setConfirmation(null);
    setLoading(false);
    dirty.current = false;
    sessionStorage.removeItem('evaluation-subject');
  }
  async function authenticate(handoff = false) {
    const epoch = ++authEpoch.current;
    setAuthLoading(true);
    setAuthError('');
    try {
      if (handoff && window.opener) {
        const config = await fetch('/evaluation/api/client-config').then((r) =>
          r.ok ? r.json() : { trusted_origins: [] },
        );
        const tokens = await receiveHandoff({
          opener: window.opener,
          trusted: config.trusted_origins || [],
          here: location.origin,
          target: window,
        });
        if (tokens && epoch === authEpoch.current) saveLogin(tokens);
      }
      if (epoch !== authEpoch.current || !localStorage.getItem(TOKEN)) return;
      const value = await api('/session');
      if (epoch === authEpoch.current) {
        sessionStorage.setItem('evaluation-subject', value.subject);
        setSession({
          ...value,
          judge: { ...value.judge, configured: value.judge_configured ?? value.judge?.configured },
        });
      }
    } catch (e) {
      if (epoch === authEpoch.current && e.status !== 401) setAuthError(e.message);
    } finally {
      if (epoch === authEpoch.current) setAuthLoading(false);
    }
  }
  async function refreshRuns() {
    if (listLock.current) return;
    const epoch = authEpoch.current;
    listLock.current = true;
    setLoading(true);
    try {
      const items = await allPages('/package-tasks');
      if (epoch === authEpoch.current) {
        setRuns(taskRows(items));
        setLastSync(new Date());
        setSyncError('');
      }
    } catch (e) {
      if (epoch === authEpoch.current) setSyncError(e.message);
    } finally {
      if (epoch === authEpoch.current) {
        listLock.current = false;
        setLoading(false);
      }
    }
  }
  async function refreshDefinitions() {
    const epoch = authEpoch.current;
    const [s, d, v] = await Promise.all([allPages('/eval-sets'), allPages('/data-sources'), allPages('/solver-sets')]);
    if (epoch === authEpoch.current) {
      setSets(s);
      setSources(d);
      setSolverSets(v);
    }
  }
  useEffect(() => {
    authenticate(true);
    const changed = (e) => {
      if (e.key == null || [TOKEN, REFRESH].includes(e.key)) {
        reset();
        authenticate();
      }
    };
    const expired = () => {
      reset();
      setAuthLoading(false);
      setAuthError('登录已过期，请重新认证后继续查看');
    };
    window.addEventListener('storage', changed);
    window.addEventListener('evaluation-auth-required', expired);
    return () => {
      authEpoch.current += 1;
      window.removeEventListener('storage', changed);
      window.removeEventListener('evaluation-auth-required', expired);
    };
  }, []);
  useEffect(() => {
    if (!session) return;
    refreshRuns();
    refreshDefinitions().catch((e) => setSyncError(e.message));
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') refreshRuns();
    }, 5000);
    return () => clearInterval(timer);
  }, [session?.subject]);
  function applyRoute(hash) {
    if (route.page === 'tasks') listScroll.current = contentRef.current?.scrollTop || 0;
    routeRef.current = hash;
    setRoute(parseRoute(hash));
  }
  useEffect(() => {
    const change = () => {
      if (dirty.current) {
        const target = location.hash;
        history.replaceState(null, '', `${location.pathname}${location.search}${routeRef.current}`);
        setLeave(target);
      } else applyRoute(location.hash);
    };
    const unload = (e) => {
      if (dirty.current) {
        e.preventDefault();
        e.returnValue = '';
      }
    };
    window.addEventListener('hashchange', change);
    window.addEventListener('beforeunload', unload);
    return () => {
      window.removeEventListener('hashchange', change);
      window.removeEventListener('beforeunload', unload);
    };
  }, [route.page]);
  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = route.page === 'tasks' ? listScroll.current : 0;
  }, [route.page, route.id]);
  function navigate(path) {
    const hash = `#${path === '/' ? '/' : path}`;
    if (dirty.current) setLeave(hash);
    else {
      if (location.hash === hash) applyRoute(hash);
      else location.hash = hash;
    }
  }
  async function doAction(type, run, after) {
    if (type === 'delete') await api(`/runs/${run.id}`, undefined, 'DELETE');
    else await api(`/runs/${run.id}/${type === 'resume' ? 'resume' : 'cancel'}`, {});
    await refreshRuns();
    if (type === 'delete' && route.page === 'run') navigate('/');
    else await after?.();
  }
  function onAction(type, run, after) {
    setConfirmation({ type, run, after });
  }
  function logout() {
    clearLogin();
    reset();
    setAuthLoading(false);
  }
  const active = runs.filter((r) => ACTIVE.has(r.status)).length,
    nav = [
      ['tasks', '/', 'task', '评测任务'],
      ['sets', '/eval-sets', 'set', '评测集'],
      ['sources', '/data-sources', 'db', '数据源'],
      // Consumer feedback, when the deployment enabled it.
      ...(session?.feedback_enabled ? [['feedback', '/feedback', 'message', '用户反馈']] : []),
    ];
  if (!session)
    return (
      <div className="login-page">
        <section className="login-intro">
          <div className="brand">
            <img className="brand-icon" src={brandIconUrl} alt="" />
            <b>知识包评测</b>
          </div>
          <h1>
            每一次评测，
            <br />
            都能继续查看。
          </h1>
          <p>
            使用统一账号登录，查看任务状态、执行进度与结果。
            <br />
            待授权或已中断的任务，可在详情中确认恢复。
          </p>
          <div className="login-journey">
            <span>统一账号登录</span>
            <Icon name="arrow" />
            <span>评测任务</span>
            <Icon name="arrow" />
            <span>执行详情</span>
          </div>
        </section>
        <section className="login-card">
          <h2>登录统一账号</h2>
          <p>
            从建模页面进入时可复用有效登录。
            <br />
            登录过期后，请重新认证。
          </p>
          {authLoading ? (
            <Skeleton />
          ) : (
            <Login
              error={authError}
              returnToRun={route.page === 'run'}
              onLogin={async (data) => {
                saveLogin(data);
                await authenticate();
              }}
            />
          )}
        </section>
      </div>
    );
  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={() => navigate('/')}>
          <img className="brand-icon" src={brandIconUrl} alt="" />
          <b>知识包评测</b>
          <span>EVALUATION</span>
        </button>
        <div className="topbar-actions">
          <Badge tone={session.judge?.configured ? 'green' : 'amber'}>
            {session.judge?.configured ? '评测判分模型已配置' : '评测判分模型未配置'}
          </Badge>
          <span className="topbar-divider" />
          <UserMenu
            user={session.user}
            onLogout={() => {
              if (dirty.current) setConfirmation({ type: 'logout' });
              else logout();
            }}
          />
        </div>
      </header>
      <aside className="sidebar">
        <div className="side-caption">评测工作台</div>
        <nav aria-label="主要导航">
          {nav.map(([page, path, icon, label]) => (
            <button
              key={page}
              title={label}
              className={`nav-item ${route.page === page || (page === 'tasks' && ['run', 'new'].includes(route.page)) || (page === 'sets' && route.page === 'solverSets') ? 'selected' : ''}`}
              aria-current={route.page === page ? 'page' : undefined}
              onClick={() => navigate(path)}
            >
              <Icon name={icon} />
              <span>{label}</span>
              {page === 'tasks' && active > 0 && <em>{active}</em>}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button
            className={`nav-item ${route.page === 'settings' ? 'selected' : ''}`}
            title="评测判分模型配置"
            onClick={() => navigate('/settings')}
          >
            <Icon name="settings" />
            <span>评测判分模型配置</span>
          </button>
          <div className="sidebar-help">
            <b>回到任务，继续查看</b>
            <p>
              关闭页面后，可从「评测任务」
              <br />
              找回运行记录和执行详情。
            </p>
          </div>
        </div>
      </aside>
      <main className={`content ${route.page === 'new' ? 'wizard-content' : ''}`} ref={contentRef}>
        {syncError && route.page !== 'run' && (
          <Alert
            tone="amber"
            action={
              <Button
                onClick={() => {
                  refreshRuns();
                  refreshDefinitions().catch((e) => setSyncError(e.message));
                }}
              >
                重试同步
              </Button>
            }
          >
            同步未完成，保留上次成功数据。{syncError}
          </Alert>
        )}
        {route.page === 'tasks' && (
          <TaskCenter
            runs={runs}
            loading={loading}
            lastSync={lastSync}
            onRefresh={refreshRuns}
            navigate={navigate}
            searchState={searchState}
            setSearchState={setSearchState}
            onAction={onAction}
          />
        )}
        {route.page === 'new' && (
          <Wizard
            key={`${session.subject}-${route.from || ''}-${route.release || ''}`}
            sets={sets}
            solverSets={solverSets}
            sources={sources}
            runs={runs}
            session={session}
            refreshDefinitions={() => refreshDefinitions().catch((e) => setSyncError(e.message))}
            navigate={navigate}
            from={route.from}
            release={route.release}
            registerDirty={(value) => {
              dirty.current = value;
            }}
          />
        )}
        {route.page === 'run' && (
          <RunDetail
            key={`${session.subject}-${route.id}`}
            id={route.id}
            navigate={navigate}
            onAction={onAction}
            onChanged={refreshRuns}
          />
        )}
        {['sets', 'solverSets', 'sources'].includes(route.page) && (
          <Definitions
            key={route.page}
            kind={route.page}
            id={route.id}
            items={{ sets, solverSets, sources }[route.page]}
            counts={{ sets: sets.length, solverSets: solverSets.length }}
            refresh={refreshDefinitions}
            navigate={navigate}
            registerDirty={(value) => {
              dirty.current = value;
            }}
          />
        )}
        {route.page === 'feedback' && (
          <Suspense fallback={<Skeleton />}>
            {route.id ? (
              <FeedbackRun key={`${session.subject}-${route.id}`} id={route.id} navigate={navigate} />
            ) : (
              <FeedbackList key={session.subject} navigate={navigate} />
            )}
          </Suspense>
        )}
        {route.page === 'settings' && (
          <>
            <Heading title="评测判分模型配置">默认使用管理员维护的默认配置；取消“使用默认配置”可保存你自己的判分模型，默认配置启用时随时可切回。</Heading>
            <JudgeSettings
              onChanged={async () => {
                const value = await api('/session');
                setSession({ ...value, judge: { ...value.judge, configured: value.judge_configured } });
              }}
            />
          </>
        )}
      </main>
      {confirmation && (
        <Confirm
          title={
            confirmation.type === 'logout'
              ? '退出并放弃未保存修改？'
              : confirmation.type === 'delete'
                ? '删除评测记录？'
                : confirmation.type === 'resume'
                  ? '确认恢复原评测任务？'
                  : '停止当前评测？'
          }
          label={
            confirmation.type === 'logout'
              ? '退出登录'
              : confirmation.type === 'delete'
                ? '删除评测记录'
                : confirmation.type === 'resume'
                  ? '授权并继续'
                  : '停止评测'
          }
          danger={['delete', 'cancel', 'logout'].includes(confirmation.type)}
          onClose={() => setConfirmation(null)}
          onConfirm={() =>
            confirmation.type === 'logout'
              ? logout()
              : doAction(confirmation.type, confirmation.run, confirmation.after)
          }
        >
          <p>
            {confirmation.type === 'logout'
              ? '尚未保存的编辑将丢失。已创建的任务仍可在重新登录后查看。'
              : confirmation.type === 'delete'
                ? `任务 ${shortId(confirmation.run.id)} 的运行记录、自有消费空间及隔离评测数据会被清理，不删除外部图库或 SQL 数据。未结束的任务须确认 consumer 侧没有仍在执行的问题。`
                : confirmation.type === 'resume'
                  ? '使用当前有效登录继续原任务，先核对远端状态；已完成的回答不会重复执行。'
                  : '保留已经完成的回答与判分。停止提交后等待远端确认与对账，再显示已停止。'}
          </p>
        </Confirm>
      )}
      {leave !== null && (
        <Confirm
          title="放弃未保存的修改并离开？"
          label="离开页面"
          onClose={() => setLeave(null)}
          onConfirm={async () => {
            dirty.current = false;
            if (location.hash === leave) applyRoute(leave);
            else location.hash = leave;
          }}
        >
          <p>尚未保存的修改将丢失，已保存的任务与配置保留。</p>
        </Confirm>
      )}
    </div>
  );
}

function Login({ onLogin, error: authError, returnToRun }) {
  const { busy, error, act } = useAction();
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const form = e.currentTarget,
          data = new FormData(form);
        act(async () => {
          const tokens = await api('/auth/login', {
            username: data.get('username'),
            password: data.get('password'),
          });
          form.elements.password.value = '';
          await onLogin(tokens);
        });
      }}
    >
      {returnToRun && <Alert title="登录后返回原任务">任务状态将在身份验证成功后加载。</Alert>}
      {(error || authError) && <Alert tone="red">{error || authError}</Alert>}
      <label className="field">
        <span>账号</span>
        <input name="username" autoComplete="username" required placeholder="请输入账号" />
      </label>
      <label className="field">
        <span>密码</span>
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          required
          placeholder="请输入密码"
        />
      </label>
      <Button type="submit" variant="primary" busy={busy} icon="arrow">
        {returnToRun ? '登录并继续查看' : '登录评测工作台'}
      </Button>
      <p className="help-text">仅展示当前账号有权访问的任务。</p>
    </form>
  );
}

function Definitions({ kind, id, items, counts, refresh, navigate, registerDirty }) {
  const solver = kind === 'solverSets',
    sets = kind === 'sets',
    cases = solver || sets,
    path = solver ? '/solver-sets' : sets ? '/eval-sets' : '/data-sources',
    label = solver ? '求解验证集' : sets ? '评测集' : '数据源',
    pageTitle = cases ? '评测集' : '数据源',
    editorLabel = cases ? label : '数据源配置';
  const [editing, setEditing] = useState(null),
    [deleting, setDeleting] = useState(null),
    [search, setSearch] = useState(''),
    [loadError, setLoadError] = useState(''),
    [retry, setRetry] = useState(0);
  const dirtyRef = useRef(registerDirty);
  dirtyRef.current = registerDirty;
  useEffect(() => {
    let live = true;
    setEditing(null);
    setLoadError('');
    dirtyRef.current(false);
    if (id === 'new') setEditing({ routeId: id, record: {} });
    else if (id)
      api(`${path}/${id}`)
        .then((item) => {
          if (live) setEditing({ routeId: id, record: item });
        })
        .catch((e) => {
          if (live) setLoadError(e.message);
        });
    return () => {
      live = false;
      dirtyRef.current(false);
    };
  }, [id, path, retry]);
  const filtered = items.filter((i) => i.name.toLowerCase().includes(search.toLowerCase()));
  function closeEditor() {
    dirtyRef.current(false);
    navigate(path);
  }
  if (id) {
    const record = editing?.routeId === id ? editing.record : null;
    if (!record)
      return (
        <>
          <Breadcrumb
            parent={pageTitle}
            current={`${id === 'new' ? '新建' : '编辑'}${editorLabel}`}
            onBack={closeEditor}
          />
          <Heading title={`${id === 'new' ? '新建' : '编辑'}${editorLabel}`} />
          {loadError ? (
            <Empty
              title="配置暂时无法加载"
              action={<Button onClick={() => setRetry((n) => n + 1)}>重试</Button>}
            >
              {loadError}
            </Empty>
          ) : (
            <Skeleton />
          )}
        </>
      );
    if (solver)
      return (
        <SolverCaseEditor
          key={id}
          record={record.id ? record : null}
          onDirty={registerDirty}
          onDiscard={() => dirtyRef.current(false)}
          onClose={closeEditor}
          onSaved={async () => {
            await refresh();
            closeEditor();
          }}
        />
      );
    return sets ? (
      <CaseEditor
        key={id}
        record={record.id ? record : null}
        onDirty={registerDirty}
        onDiscard={() => dirtyRef.current(false)}
        onClose={closeEditor}
        onSaved={async () => {
          await refresh();
          closeEditor();
        }}
      />
    ) : (
      <SourceEditor
        key={id}
        record={record.id ? record : null}
        onDirty={registerDirty}
        onClose={closeEditor}
        onSaved={async (item, close) => {
          await refresh();
          if (close) closeEditor();
        }}
      />
    );
  }
  return (
    <>
      <Heading
        title={pageTitle}
        actions={
          <Button variant="primary" icon="plus" onClick={() => navigate(`${path}/new`)}>
            新建{label}
          </Button>
        }
      >
        {solver
          ? '管理可复用的求解验证用例，已有任务保留原用例快照。'
          : sets
            ? '管理可复用的问题与参考答案，已有任务保留原用例快照。'
            : '保存并复用连接与数据文件，按 Provider 配置评测数据源。'}
      </Heading>
      <section className="panel table-panel">
        <div className="table-toolbar">
          {cases ? (
            <div className="tabs">
              {[
                ['sets', '/eval-sets', '数据评测集'],
                ['solverSets', '/solver-sets', '求解验证集'],
              ].map(([value, target, title]) => (
                <button
                  key={value}
                  type="button"
                  className={kind === value ? 'selected' : ''}
                  aria-pressed={kind === value}
                  onClick={() => navigate(target)}
                >
                  {title} {counts[value]}
                </button>
              ))}
            </div>
          ) : (
            <strong>{items.length} 个数据源</strong>
          )}
          <label className="search-input">
            <Icon name="search" />
            <input
              aria-label="搜索配置名称"
              placeholder="搜索名称"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
        </div>
        {!filtered.length ? (
          <Empty
            title={items.length ? '没有匹配的配置' : `还没有${label}`}
            action={
              <Button icon="plus" onClick={() => navigate(`${path}/new`)}>
                新建配置
              </Button>
            }
          >
            保存后可以在新建评测中直接选择。
          </Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>名称</th>
                  <th>{cases ? '用例数量' : '类型与范围'}</th>
                  <th>修订</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <strong>{item.name}</strong>
                    </td>
                    <td>
                      {cases ? `${item.case_count ?? item.cases?.length ?? 0} 条用例` : sourceSummary(item)}
                    </td>
                    <td>r{item.revision}</td>
                    <td>
                      <div className="actions">
                        <Button
                          variant="text"
                          onClick={() => navigate(`${path}/${item.id}`)}
                        >
                          编辑
                        </Button>
                        <Button variant="text danger" onClick={() => setDeleting(item)}>
                          删除
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {deleting && (
        <Confirm
          title={`删除${label}定义？`}
          label="删除定义"
          danger
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await api(`${path}/${deleting.id}`, undefined, 'DELETE');
            await refresh();
          }}
        >
          <p>
            删除“{deleting.name}”的已保存定义{cases ? '' : '与连接凭据'}
            。已有任务仍使用原配置快照，历史报告不会被删除。
          </p>
        </Confirm>
      )}
    </>
  );
}
