import { useEffect, useRef, useState } from 'react';
import { packageChat, packageChatApply } from './api.js';
import { useElapsed } from './ui.jsx';

// 知识包完善右栏：修缮对话（两段式）。
// 发消息 → agent 影响分析：咨询直接回答；修改类返回计划卡片（改哪些对象/操作 + 风险）→ 专家确认 → 应用落新版本。
const chatKey = (projectId) => `mw_pchat_${projectId}`;
const loadTurns = (projectId) => {
  try { return JSON.parse(sessionStorage.getItem(chatKey(projectId)) || '[]'); } catch { return []; }
};

export default function PackageChat({ projectId, packageDraft, busy, onBusyChange }) {
  // 会话存 sessionStorage：切页/回项目列表再进来对话不丢（关浏览器标签才清）
  const [turns, setTurns] = useState(() => loadTurns(projectId));
  const [input, setInput] = useState('');
  const sec = useElapsed(!!busy);
  const bodyRef = useRef(null);

  useEffect(() => { setTurns(loadTurns(projectId)); }, [projectId]);
  useEffect(() => {
    try { sessionStorage.setItem(chatKey(projectId), JSON.stringify(turns.slice(-40))); } catch { /* 容量满忽略 */ }
  }, [projectId, turns]);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: 'smooth' });
  }, [turns, busy]);

  // 体检面板「交给修缮对话」预填
  useEffect(() => {
    const h = (e) => setInput(e.detail || '');
    window.addEventListener('mw-chat-prefill', h);
    return () => window.removeEventListener('mw-chat-prefill', h);
  }, []);

  const historyForApi = () => turns.filter((t) => t.text).map((t) => ({ role: t.role, text: t.text }));

  const send = async () => {
    const message = input.trim();
    if (!message || busy || packageDraft) return;
    setInput('');
    setTurns((t) => [...t, { role: 'user', text: message }]);
    onBusyChange('plan');
    try {
      const d = await packageChat(projectId, message, historyForApi());
      if (d.plan) {
        setTurns((t) => [...t, { role: 'assistant', text: '', plan: d.plan, planMessage: message, planState: 'pending' }]);
      } else {
        setTurns((t) => [...t, { role: 'assistant', text: d.reply }]);
      }
    } catch (e) {
      setTurns((t) => [...t, { role: 'assistant', text: `出错了：${e.message}` }]);
    } finally { onBusyChange(''); }
  };

  const confirmPlan = async (idx) => {
    if (busy || packageDraft) return;
    const turn = turns[idx];
    onBusyChange('apply');
    try {
      const d = await packageChatApply(projectId, turn.planMessage, historyForApi(), turn.plan);
      setTurns((t) => t.map((x, i) => (i === idx ? { ...x, planState: 'applied' } : x))
        .concat([{ role: 'assistant', text: d.reply, version: d.version }]));
      if (d.version) window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (e) {
      setTurns((t) => [...t, { role: 'assistant', text: `应用失败：${e.message}` }]);
    } finally { onBusyChange(''); }
  };

  const cancelPlan = (idx) =>
    setTurns((t) => t.map((x, i) => (i === idx ? { ...x, planState: 'cancelled' } : x)));

  return (
    <div className="pchat">
      <div className="ctx-tabs"><button className="on">🛠 包修缮对话</button></div>
      <div className="pchat-body" ref={bodyRef}>
        {packageDraft && (
          <div className="draft-chat-gate">
            <b>人工编辑草稿尚未发布</b>
            <span>请先在“知识包完善”中发布或放弃草稿，再使用修缮对话。</span>
          </div>
        )}
        {!turns.length && (
          <div className="pchat-hint">
            用自然语言修缮知识包：先出<b>影响分析计划</b>，确认后落新版本。<br />
            例：「处理成本只算物料成本，写进相关操作」
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`pchat-msg ${t.role}`}>
            <div className="bubble">
              {t.text}
              {t.plan && (
                <div className="plan-card">
                  <div className="plan-title">📋 修缮计划（待确认）</div>
                  <div className="plan-sum">{t.plan.summary}</div>
                  {t.plan.changes.map((c, j) => (
                    <div key={j} className="plan-chg">
                      <span className={`tag ${c.action === 'remove' ? 'tag-err' : c.action === 'add' ? 'tag-ok' : 'tag-lang'}`}>
                        {{ add: '新增', modify: '修改', remove: '删除' }[c.action]}
                      </span>
                      <span className="plan-key">{c.targetKind === 'object' ? '对象' : '操作'} {c.targetKey}</span>
                      <div className="plan-desc">{c.description}</div>
                    </div>
                  ))}
                  {t.plan.risks?.length > 0 && (
                    <div className="plan-risks">⚠ 风险：{t.plan.risks.join('；')}</div>
                  )}
                  {t.planState === 'pending' && (
                    <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                      <button className="btn btn-p" style={{ padding: '4px 14px', fontSize: 12 }} disabled={!!busy || !!packageDraft}
                        onClick={() => confirmPlan(i)}>✔ 确认应用</button>
                      <button className="btn" style={{ padding: '4px 14px', fontSize: 12 }} disabled={!!busy}
                        onClick={() => cancelPlan(i)}>取消</button>
                    </div>
                  )}
                  {t.planState === 'applied' && <div className="pchat-ver">✓ 已应用</div>}
                  {t.planState === 'cancelled' && <div style={{ marginTop: 6, fontSize: 11.5, color: 'var(--muted)' }}>已取消</div>}
                </div>
              )}
              {t.version && <div className="pchat-ver">✓ 已生成 v{t.version}</div>}
            </div>
          </div>
        ))}
        {busy && (
          <div className="pchat-msg assistant">
            <div className="bubble"><span className="spinner" /> {busy === 'plan' ? '影响分析中' : '应用计划中（生成新版本）'}… 已等待 {sec} 秒</div>
          </div>
        )}
      </div>
      <div className="pchat-input">
        <textarea rows={2} value={input}
          disabled={!!packageDraft}
          placeholder={packageDraft ? '请先发布或放弃人工编辑草稿' : '描述要修缮的内容，Enter 发送（Shift+Enter 换行）'}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="btn btn-p" disabled={!!busy || !!packageDraft || !input.trim()} onClick={send}>发送</button>
      </div>
    </div>
  );
}
