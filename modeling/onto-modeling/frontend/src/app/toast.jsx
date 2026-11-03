// 全局轻提示：样式同设计稿 #toast
import { useEffect, useState } from 'react';
import { Icon } from '../ui/index.jsx';

const listeners = new Set();
export function toast(message, icon = 'check') {
  listeners.forEach((fn) => fn({ message, icon, at: Date.now() }));
}

export function ToastHost() {
  const [t, setT] = useState(null);
  const [show, setShow] = useState(false);
  useEffect(() => {
    let timer;
    const fn = (x) => {
      setT(x);
      setShow(true);
      clearTimeout(timer);
      timer = setTimeout(() => setShow(false), 1800);
    };
    listeners.add(fn);
    return () => { listeners.delete(fn); clearTimeout(timer); };
  }, []);
  return (
    <div id="toast" className={show ? 'show' : ''} role="status">
      {t ? <><Icon name={t.icon} size={14} />{t.message}</> : null}
    </div>
  );
}
