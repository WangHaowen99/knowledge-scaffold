import { useEffect, useRef, useState } from 'react';
import { getToken, getRefreshToken, clearSession, me, importPackageWorkspace } from './api.js';
import LoginPage from './LoginPage.jsx';
import ProjectList from './ProjectList.jsx';
import Workbench from './Workbench.jsx';
import SettingsModal from './SettingsModal.jsx';
import Tour, { PROJECT_STEPS } from './Tour.jsx';

export default function App() {
  const [status, setStatus] = useState('checking'); // checking | out | in
  const [user, setUser] = useState(null);
  const [projectId, setProjectId] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState('llm');
  const [menuOpen, setMenuOpen] = useState(false);
  const [importingPackage, setImportingPackage] = useState(false);
  const [importError, setImportError] = useState('');
  const [importNotice, setImportNotice] = useState('');
  const userRef = useRef(null);
  const packageInputRef = useRef(null);

  useEffect(() => {
    if (!getToken()) { setStatus('out'); return; }
    me().then((d) => { setUser(d.user); setStatus('in'); })
      .catch((e) => {
        // 只在明确鉴权拒绝且静默续期链已放弃（refresh token 已被 authFetch 清掉/不存在）时清凭据；
        // 瞬时故障（网络错、5xx、认证服务暂不可用）保留凭据，服务恢复后仍可续期
        if ((e.status === 401 || e.status === 403) && !getRefreshToken()) clearSession();
        setStatus('out');
      });
  }, []);

  // 点击菜单外任意处 / 按 Esc 关闭用户菜单。
  // 用捕获阶段 + contains 判断：页面里有组件对 click 调 stopPropagation（如优化建议卡的勾选），冒泡监听会被吞
  useEffect(() => {
    if (!menuOpen) return;
    const close = (e) => { if (!userRef.current?.contains(e.target)) setMenuOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false); };
    document.addEventListener('pointerdown', close, true);
    window.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('pointerdown', close, true); window.removeEventListener('keydown', onKey); };
  }, [menuOpen]);

  const logout = () => { clearSession(); setStatus('out'); setUser(null); setProjectId(null); setMenuOpen(false); };

  const importPackage = async (file) => {
    if (!file) return;
    setImportingPackage(true); setImportError(''); setImportNotice('');
    try {
      const result = await importPackageWorkspace(file);
      setImportNotice((result.warnings ?? []).join('；'));
      setProjectId(result.project.id);
    } catch (error) {
      setImportError(error instanceof Error ? error.message : '知识包导入失败');
    } finally {
      setImportingPackage(false);
    }
  };

  // 操作引导联动：引导步骤可打开配置中心并切换 tab；引导结束时收起弹窗
  useEffect(() => {
    const open = (e) => { setSettingsTab(e.detail?.tab || 'llm'); setSettingsOpen(true); };
    const end = () => setSettingsOpen(false);
    window.addEventListener('mw-settings-open', open);
    window.addEventListener('mw-tour-end', end);
    return () => { window.removeEventListener('mw-settings-open', open); window.removeEventListener('mw-tour-end', end); };
  }, []);

  if (status === 'checking') {
    return <div className="mwr" style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', color: '#898781' }}>加载中…</div>;
  }
  if (status === 'out') {
    return <LoginPage onLogin={(u) => { setUser(u); setStatus('in'); }} />;
  }

  return (
    <div className="mwr shell">
      <div className="topbar">
        <div className="logo"><span className="mk">知</span>知识包建模工具</div>
        <span className="crumb">
          {projectId
            ? <><a onClick={() => setProjectId(null)}>项目</a> / 建模工作台</>
            : '项目'}
        </span>
        <div className="right">
          {!projectId && (
            <>
              <button className="tb-btn" disabled={importingPackage}
                onClick={() => packageInputRef.current?.click()}>
                {importingPackage ? '导入中…' : '导入已有知识包'}
              </button>
              <input ref={packageInputRef} type="file" accept=".zip" hidden
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = '';
                  void importPackage(file);
                }} />
            </>
          )}
          <button className="tb-btn" title="功能引导"
            onClick={() => window.dispatchEvent(new CustomEvent('mw-tour-start', { detail: { scene: projectId ? 'workbench' : 'projects' } }))}>❓ 引导</button>
          <button className="tb-btn" data-tour="config" onClick={() => setSettingsOpen(true)}>⚙ 配置中心</button>
          <span className="tb-sep" />
          <div className="tb-user" ref={userRef}>
            <button className={`tb-chip ${menuOpen ? 'open' : ''}`} title="账号菜单"
              onClick={() => setMenuOpen((v) => !v)}>
              <span className="tb-avatar">{(user?.username || '?').slice(0, 1).toUpperCase()}</span>
              <span className="tb-name">{user?.username}</span>
              <span className={`tb-caret ${menuOpen ? 'up' : ''}`}>▾</span>
            </button>
            {menuOpen && (
              <div className="tb-menu">
                <div className="tb-menu-head">
                  <span className="tb-avatar lg">{(user?.username || '?').slice(0, 1).toUpperCase()}</span>
                  <div>
                    <div className="tb-menu-name">{user?.username}</div>
                    <div className="tb-menu-sub">当前登录账号</div>
                  </div>
                </div>
                <button className="tb-menu-item" onClick={logout}>⎋ 退出登录</button>
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="main">
        {!projectId && importError && <div className="lerr">知识包导入失败：{importError}</div>}
        {!projectId && importNotice && <div className="saved">{importNotice}</div>}
        {projectId
          ? <Workbench projectId={projectId} onBack={() => setProjectId(null)} />
          : <ProjectList onOpen={setProjectId} />}
      </div>
      {!projectId && <Tour scene="projects" steps={PROJECT_STEPS} />}
      {settingsOpen && <SettingsModal tab={settingsTab} onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
