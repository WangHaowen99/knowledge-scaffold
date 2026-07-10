import { useEffect, useState } from 'react';
import { getToken, getRefreshToken, clearSession, me } from './api.js';
import LoginPage from './LoginPage.jsx';
import ProjectList from './ProjectList.jsx';
import Workbench from './Workbench.jsx';
import SettingsModal from './SettingsModal.jsx';

export default function App() {
  const [status, setStatus] = useState('checking'); // checking | out | in
  const [user, setUser] = useState(null);
  const [projectId, setProjectId] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

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
          {projectId && (
            <button className="btn" style={{ padding: '4px 12px', fontSize: 12 }} title="功能引导"
              onClick={() => window.dispatchEvent(new CustomEvent('mw-tour-start'))}>❓ 引导</button>
          )}
          <button className="btn" data-tour="config" style={{ padding: '4px 12px', fontSize: 12 }} onClick={() => setSettingsOpen(true)}>⚙ 配置中心</button>
          <span>{user?.username}</span>
          <button className="btn" style={{ padding: '4px 12px', fontSize: 12 }}
            onClick={() => { clearSession(); setStatus('out'); setUser(null); setProjectId(null); }}>退出</button>
        </div>
      </div>
      <div className="main">
        {projectId
          ? <Workbench projectId={projectId} onBack={() => setProjectId(null)} />
          : <ProjectList onOpen={setProjectId} />}
      </div>
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
