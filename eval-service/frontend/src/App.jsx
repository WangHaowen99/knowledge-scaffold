import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { api, allPages, TOKEN, REFRESH, clearLogin, saveLogin } from './api.js';
import { receiveHandoff } from './handoff.js';
import { ACTIVE, parseRoute, shortId, sourceSummary, taskRows } from './domain.js';
import { Alert, Badge, Breadcrumb, Button, Confirm, Empty, Heading, Icon, Skeleton, useAction } from './ui.jsx';
import { TaskCenter } from './TaskCenter.jsx';
import { Wizard } from './Wizard.jsx';
import { RunDetail } from './RunDetail.jsx';
import { CaseEditor } from './CaseEditor.jsx';
import { SourceEditor } from './SourceEditor.jsx';
import { JudgeSettings } from './JudgeSettings.jsx';
import { UserMenu } from './UserMenu.jsx';

// Loaded on first use: the page only exists where the deployment enabled consumer feedback.
const FeedbackList = lazy(() => import('./FeedbackPage.jsx').then((module) => ({ default: module.FeedbackList })));
const FeedbackRun = lazy(() => import('./FeedbackPage.jsx').then((module) => ({ default: module.FeedbackRun })));
const FeedbackPackage = lazy(() => import('./FeedbackPage.jsx').then((module) => ({ default: module.FeedbackPackage })));

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
    [feedbackWaiting, setFeedbackWaiting] = useState(0),
    [sets, setSets] = useState([]),
    [sources, setSources] = useState([]),
    [loading, setLoading] = useState(false),
    [syncError, setSyncError] = useState(''),
    [lastSync, setLastSync] = useState(null);
  const [searchState, setSearchState] = useState({ tab: 'all', search: '' }),
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
    setFeedbackWaiting(0);
    setSets([]);
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
  // Packages with feedback waiting for an owner, counted on the navigation; failures leave the last count.
  async function refreshFeedback() {
    const epoch = authEpoch.current;
    const page = await api('/package-tasks?origin=consumer_feedback');
    if (epoch === authEpoch.current)
      setFeedbackWaiting(page.items.filter((item) => item.status === 'pending').length);
  }
  async function refreshDefinitions() {
    const epoch = authEpoch.current;
    const [s, d] = await Promise.all([allPages('/eval-sets'), allPages('/data-sources')]);
    if (epoch === authEpoch.current) {
      setSets(s);
      setSources(d);
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
    const feedback = () => session.feedback_enabled && refreshFeedback().catch(() => {});
    refreshRuns();
    feedback();
    refreshDefinitions().catch((e) => setSyncError(e.message));
    const timer = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      refreshRuns();
      feedback();
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
      ['评测', [
        ['tasks', '/', 'task', '评测任务', active, ''],
        // Consumer feedback, when the deployment enabled it.
        ...(session?.feedback_enabled ? [['feedback', '/feedback', 'message', '用户反馈', feedbackWaiting, 'amber']] : []),
      ]],
      ['配置', [
        ['sets', '/eval-sets', 'set', '评测集'],
        ['sources', '/data-sources', 'db', '数据源'],
        ['settings', '/settings', 'settings', '判分模型'],
      ]],
    ],
    crumbs = pageCrumbs(route);
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
      <aside className="sidebar">
        <button className="brand" onClick={() => navigate('/')}>
          <img className="brand-icon" src={brandIconUrl} alt="" />
          <b>知识包评测</b>
        </button>
        <nav aria-label="主要导航">
          {nav.map(([group, items]) => (
            <React.Fragment key={group}>
              <div className="nav-group">{group}</div>
              {items.map(([page, path, icon, label, count, tone]) => (
                <button
                  key={page}
                  title={label}
                  className={`nav-item ${route.page === page || (page === 'tasks' && ['run', 'new'].includes(route.page)) ? 'selected' : ''}`}
                  aria-current={route.page === page ? 'page' : undefined}
                  onClick={() => navigate(path)}
                >
                  <Icon name={icon} />
                  <span>{label}</span>
                  {count > 0 && (
                    <em className={tone} aria-label={page === 'tasks' ? `${count} 个评测进行中` : `${count} 个知识包有待确认的反馈`}>
                      {count}
                    </em>
                  )}
                </button>
              ))}
            </React.Fragment>
          ))}
        </nav>
      </aside>
      <div className="main-column">
      <header className="topbar">
        <nav className="crumbs" aria-label="当前位置">
          {crumbs.map(([label, path], i) =>
            path ? (
              <React.Fragment key={label}>
                <button type="button" className="text-link" onClick={() => navigate(path)}>
                  {label}
                </button>
                <span aria-hidden="true">/</span>
              </React.Fragment>
            ) : (
              <b key={label} aria-current={i === crumbs.length - 1 ? 'page' : undefined}>
                {label}
              </b>
            ),
          )}
        </nav>
        <div className="topbar-actions">
          <Badge tone={session.judge?.configured ? 'green' : 'amber'}>
            {session.judge?.configured ? '判分模型已配置' : '判分模型未配置'}
          </Badge>
          <UserMenu
            user={session.user}
            onLogout={() => {
              if (dirty.current) setConfirmation({ type: 'logout' });
              else logout();
            }}
          />
        </div>
      </header>
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
        {['sets', 'sources'].includes(route.page) && (
          <Definitions
            key={route.page}
            kind={route.page}
            id={route.id}
            items={route.page === 'sets' ? sets : sources}
            refresh={refreshDefinitions}
            navigate={navigate}
            registerDirty={(value) => {
              dirty.current = value;
            }}
          />
        )}
        {route.page === 'feedback' && (
          <Suspense fallback={<Skeleton />}>
            {route.package ? (
              <FeedbackPackage
                key={`${session.subject}-${route.package}`}
                ontologyId={route.package}
                navigate={navigate}
                onChanged={() => refreshFeedback().catch(() => {})}
              />
            ) : route.id ? (
              <FeedbackRun key={`${session.subject}-${route.id}`} id={route.id} navigate={navigate} />
            ) : (
              <FeedbackList key={session.subject} navigate={navigate} />
            )}
          </Suspense>
        )}
        {route.page === 'settings' && (
          <>
            <Heading title="判分模型">给智能体的回答打分、生成优化建议的大模型。默认使用管理员维护的默认配置；取消“使用默认配置”可保存你自己的判分模型，默认配置启用时随时可切回。</Heading>
            <JudgeSettings
              onChanged={async () => {
                const value = await api('/session');
                setSession({ ...value, judge: { ...value.judge, configured: value.judge_configured } });
              }}
            />
          </>
        )}
      </main>
      </div>
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

// Where the page sits, for the top bar: earlier steps link back.
function pageCrumbs(route) {
  const editing = (label, path) => [[label, path], [route.id === 'new' ? `新建${label}` : `编辑${label}`]];
  if (route.page === 'run') return [['评测任务', '/'], ['执行详情']];
  if (route.page === 'new') return [['评测任务', '/'], ['新建评测']];
  if (route.page === 'sets') return route.id ? editing('评测集', '/eval-sets') : [['评测集']];
  if (route.page === 'sources') return route.id ? editing('数据源', '/data-sources') : [['数据源']];
  if (route.page === 'feedback')
    return route.package
      ? [['用户反馈', '/feedback'], ['知识包反馈']]
      : route.id
        ? [['用户反馈', '/feedback'], ['反馈详情']]
        : [['用户反馈']];
  if (route.page === 'settings') return [['判分模型']];
  return [['评测任务']];
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

function Definitions({ kind, id, items, refresh, navigate, registerDirty }) {
  const sets = kind === 'sets',
    path = sets ? '/eval-sets' : '/data-sources';
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
            parent={sets ? '评测集' : '数据源'}
            current={`${id === 'new' ? '新建' : '编辑'}${sets ? '评测集' : '数据源配置'}`}
            onBack={closeEditor}
          />
          <Heading title={`${id === 'new' ? '新建' : '编辑'}${sets ? '评测集' : '数据源配置'}`} />
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
        title={sets ? '评测集' : '数据源'}
        actions={
          <Button variant="primary" icon="plus" onClick={() => navigate(`${path}/new`)}>
            新建{sets ? '评测集' : '数据源'}
          </Button>
        }
      >
        {sets
          ? '管理可复用的问题与参考答案，已有任务保留原用例快照。'
          : '保存并复用连接与数据文件，按 Provider 配置评测数据源。'}
      </Heading>
      <div className="list-toolbar">
        <span className="muted">
          共 {items.length} 个{sets ? '评测集' : '数据源'}
        </span>
        <label className="search-input">
          <Icon name="search" />
          <input aria-label="搜索配置名称" placeholder="搜索名称" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
      </div>
      <section className="panel table-panel">
        {!filtered.length ? (
          <Empty
            title={items.length ? '没有匹配的配置' : `还没有${sets ? '评测集' : '数据源'}`}
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
            <table className="plain-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>{sets ? '用例' : '类型与范围'}</th>
                  <th>修订</th>
                  <th className="align-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => (
                  <tr key={item.id} className="clickable" onClick={() => navigate(`${path}/${item.id}`)}>
                    <td>
                      <b className="package-name">{item.name}</b>
                    </td>
                    <td className="muted-cell">
                      {sets ? `${item.case_count ?? item.cases?.length ?? 0} 条用例` : sourceSummary(item)}
                    </td>
                    <td>
                      <span className="version-tag">r{item.revision}</span>
                    </td>
                    <td className="align-right">
                      <div className="row-actions">
                        <Button
                          onClick={(e) => {
                            e.stopPropagation();
                            navigate(`${path}/${item.id}`);
                          }}
                        >
                          编辑
                        </Button>
                        <Button
                          variant="danger"
                          onClick={(e) => {
                            e.stopPropagation();
                            setDeleting(item);
                          }}
                        >
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
          title={`删除${sets ? '评测集' : '数据源'}定义？`}
          label="删除定义"
          danger
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await api(`${path}/${deleting.id}`, undefined, 'DELETE');
            await refresh();
          }}
        >
          <p>
            删除“{deleting.name}”的已保存定义{sets ? '' : '与连接凭据'}
            。已有任务仍使用原配置快照，历史报告不会被删除。
          </p>
        </Confirm>
      )}
    </>
  );
}
