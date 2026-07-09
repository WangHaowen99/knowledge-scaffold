import { useEffect, useRef, useState } from 'react';
import { packageChat } from './api.js';
import { useElapsed } from './ui.jsx';

// 知识包完善右栏：修缮对话。自然语言提修改 → agent 修订规格并落新版本；咨询类直接回答。
export default function PackageChat({ projectId }) {
  const [turns, setTurns] = useState([]); // {role, text}
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const sec = useElapsed(busy);
  const bodyRef = useRef(null);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: 'smooth' });
  }, [turns, busy]);

  const send = async () => {
    const message = input.trim();
    if (!message || busy) return;
    setInput('');
    setTurns((t) => [...t, { role: 'user', text: message }]);
    setBusy(true);
    try {
      const d = await packageChat(projectId, message, turns);
      setTurns((t) => [...t, { role: 'assistant', text: d.reply, version: d.version }]);
      if (d.version) window.dispatchEvent(new CustomEvent('mw-package-updated'));
    } catch (e) {
      setTurns((t) => [...t, { role: 'assistant', text: `出错了：${e.message}` }]);
    } finally { setBusy(false); }
  };

  return (
    <div className="pchat">
      <div className="ctx-tabs"><button className="on">🛠 包修缮对话</button></div>
      <div className="pchat-body" ref={bodyRef}>
        {!turns.length && (
          <div className="pchat-hint">
            用自然语言修缮知识包，例如：
            <ul>
              <li>「处理成本只算物料成本，不含制造成本，把这条口径写进相关操作」</li>
              <li>「给 STRUCTURE_LIST 对象补一句 Num 是单架用量的说明」</li>
              <li>「解释一下 OP-003 是干什么的」（咨询不改包）</li>
            </ul>
            要求修改时会生成新版本，左侧文件树同步更新。
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`pchat-msg ${t.role}`}>
            <div className="bubble">
              {t.text}
              {t.version && <div className="pchat-ver">✓ 已生成 v{t.version}</div>}
            </div>
          </div>
        ))}
        {busy && (
          <div className="pchat-msg assistant">
            <div className="bubble"><span className="spinner" /> 修缮中… 已等待 {sec} 秒（涉及规格修订时约 1~5 分钟）</div>
          </div>
        )}
      </div>
      <div className="pchat-input">
        <textarea rows={2} value={input} placeholder="描述要修缮的内容，Enter 发送（Shift+Enter 换行）"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="btn btn-p" disabled={busy || !input.trim()} onClick={send}>发送</button>
      </div>
    </div>
  );
}
