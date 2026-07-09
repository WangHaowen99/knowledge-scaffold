import { useEffect, useState } from 'react';
import { api } from './api.js';

// 服务设置：评测用 consumer API 地址（页面可配，存服务端）。
// 登录 issuer 只读展示（由部署 env 决定，避免把自己锁出去）。
export default function SettingsModal({ onClose }) {
  const [settings, setSettings] = useState(null);
  const [url, setUrl] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    api('/api/settings').then((d) => { setSettings(d); setUrl(d.consumerApiUrl); });
  }, []);

  const test = async () => {
    setTesting(true); setTestResult(null); setMsg('');
    try {
      setTestResult(await api('/api/settings/consumer/test', { method: 'POST', body: JSON.stringify({ url }) }));
    } catch (e) { setMsg(e.message); } finally { setTesting(false); }
  };

  const save = async () => {
    setMsg('');
    try {
      await api('/api/settings', { method: 'PUT', body: JSON.stringify({ consumerApiUrl: url }) });
      setMsg('已保存');
    } catch (e) { setMsg(e.message); }
  };

  return (
    <div className="modal-ov" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ width: 560 }}>
        <h3>服务设置</h3>
        {!settings ? <div className="empty">加载中…</div> : (
          <>
            <div className="field">
              <label>评测用 consumer API 地址（评测闭环将导入知识包并运行用例到该服务）</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <input value={url} onChange={(e) => { setUrl(e.target.value); setTestResult(null); }}
                  placeholder="http://consumer-host:5055" style={{ flex: 1 }} />
                <button className="btn" disabled={testing || !url} onClick={test}>{testing ? '测试中…' : '测试连接'}</button>
              </div>
              <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 6 }}>
                当前来源：{{ settings: '页面设置', env: '环境变量', issuer: '默认（登录 issuer）' }[settings.consumerApiUrlSource]}
              </div>
            </div>
            {testResult && (
              <div style={{ border: '1px solid var(--hair)', borderRadius: 10, padding: '10px 14px', marginBottom: 12, fontSize: 13 }}>
                <div>{testResult.reachable ? '✅ 服务可达（/health）' : '❌ 服务不可达'}</div>
                <div>{testResult.authOk ? '✅ 认证通过（当前账号在该 consumer 有效）' : '❌ 认证未通过'}</div>
                {testResult.modelReady != null && (
                  <div>{testResult.modelReady ? '✅ 模型已就绪（可运行评测）' : '⚠️ 模型未就绪（评测将失败，请先在该 consumer 配置模型）'}</div>
                )}
                {testResult.detail && <div style={{ color: 'var(--muted)', marginTop: 4 }}>{testResult.detail}</div>}
              </div>
            )}
            <div className="field">
              <label>登录认证 issuer（部署配置，只读）</label>
              <input value={settings.authIssuerUrl} disabled style={{ background: '#f7f6f3', color: 'var(--muted)' }} />
            </div>
            {msg && <div style={{ fontSize: 12.5, color: msg === '已保存' ? 'var(--good)' : 'var(--crit)', marginBottom: 8 }}>{msg}</div>}
            <div className="foot">
              <button className="btn" onClick={onClose}>关闭</button>
              <button className="btn btn-p" disabled={!url} onClick={save}>保存</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
