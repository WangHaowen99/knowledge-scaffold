import { useRef, useState } from 'react';
import { actions } from '../lib/appStore.js';
import { store } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import { formatMessageTime } from '../lib/time.js';
import Icon from '../ui/Icon.jsx';

/* 输入框永不锁死：有阻断性问题时，问题卡和自由输入并存。
   同时接受拖放——用户想补材料时最自然的位置就是他正在说话的地方。 */
export default function Composer() {
  const [drag, setDrag] = useState(false);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const demo = useStore(store, (state) => state.demo);
  const ta = useRef(null);
  const fileInput = useRef(null);

  const grow = () => {
    const el = ta.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(132, el.scrollHeight)}px`;
    el.style.overflowY = el.scrollHeight > 132 ? 'auto' : 'hidden';
  };

  const send = async () => {
    const value = draft.trim();
    if (!value || sending) return;
    if (demo) {
      actions.pushMessage({ role: 'user', at: formatMessageTime(new Date()), blocks: [{ type: 'text', text: value }] });
      setDraft('');
      requestAnimationFrame(grow);
      return;
    }
    setSending(true);
    const result = await actions.send(value);
    setSending(false);
    if (result) setDraft('');
    else ta.current?.focus();
    requestAnimationFrame(grow);
  };

  return (
    <div className={`wb-composer${drag ? ' drag' : ''}`}
      onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault(); setDrag(false);
        if (!demo && e.dataTransfer.files?.length) void actions.upload(e.dataTransfer.files);
      }}
    >
      <div className="box">
        <button className="btn btn-ghost btn-sm" type="button" title="上传资料" aria-label="上传资料"
          onClick={() => fileInput.current?.click()}><Icon name="upload" size={14} /></button>
        <input id="wb-inline-upload" ref={fileInput} type="file" multiple hidden
          accept=".md,.txt,.pdf,.doc,.docx,.xls,.xlsx,.csv,.zip"
          onChange={(event) => {
            if (!demo && event.target.files?.length) void actions.upload(event.target.files);
            event.target.value = '';
          }} />
        <textarea ref={ta} rows={1} value={draft}
          onChange={(event) => setDraft(event.target.value)} onInput={grow}
          aria-label="给建模助手发送消息" aria-keyshortcuts="Enter"
          onPaste={(event) => {
            const files = [...(event.clipboardData?.files || [])];
            if (!demo && files.length) { event.preventDefault(); void actions.upload(files); }
          }}
          placeholder={drag ? '松手即上传' : '随时插话：提问、纠正、改主意，或者直接把文件拖进来…'}
          onKeyDown={(event) => {
            if (!shouldSendComposerMessage(event)) return;
            event.preventDefault();
            void send();
          }} />
        <button className="btn btn-p btn-sm" type="button" disabled={sending || !draft.trim()}
          onClick={() => void send()} aria-label="发送">
          <Icon name="arrow" size={13} />{sending ? '发送中…' : '发送'}
        </button>
      </div>
      <div className="note">助手在服务端继续工作，关掉页面也不会中断；输入框始终可用。</div>
    </div>
  );
}

export function shouldSendComposerMessage(event) {
  return event?.key === 'Enter'
    && !event.shiftKey
    && !event.isComposing
    && !event.nativeEvent?.isComposing;
}
