// 个人设置 · 通知：偏好（A-08，PUT 整体替换），点了就存，失败退回上次存好的值。
// 系统通知要浏览器授权：打开总开关前先查 Notification 权限，没授权就先请求，拿不到不打开。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Button, Card, Note, Toggle, cx } from '../../ui/index.jsx';
import { DEFAULT_PREFS, NOTI_GROUPS, normalizePrefs, permissionNote, withPref } from './meModel.js';

export function usePrefs() {
  const { api } = useApp();
  const [st, setSt] = useState({ prefs: normalizePrefs(DEFAULT_PREFS), loaded: false, error: null });
  const saved = useRef(st.prefs); // 服务端确认过的
  const cur = useRef(st.prefs); // 界面上的（含还没存完的）
  const seq = useRef(0);

  useEffect(() => {
    let alive = true;
    api.preferences()
      .then((r) => {
        if (!alive) return;
        saved.current = normalizePrefs(r);
        cur.current = saved.current;
        setSt({ prefs: saved.current, loaded: true, error: null });
      })
      .catch((e) => alive && setSt((s) => ({ ...s, loaded: true, error: e })));
    return () => { alive = false; };
  }, [api]);

  const save = useCallback(async (path, value) => {
    const n = ++seq.current;
    const next = withPref(cur.current, path, value);
    cur.current = next;
    setSt((s) => ({ ...s, prefs: next }));
    try {
      const r = await api.savePreferences(next);
      saved.current = normalizePrefs(r);
      if (n === seq.current) {
        cur.current = saved.current;
        setSt((s) => ({ ...s, prefs: saved.current, error: null }));
      }
      return true;
    } catch (e) {
      if (n === seq.current) {
        cur.current = saved.current;
        setSt((s) => ({ ...s, prefs: saved.current }));
      }
      toast(`没有保存：${errorText(e)}`, 'alert');
      return false;
    }
  }, [api]);

  return { ...st, save };
}

function LoadNote({ error }) {
  if (!error) return null;
  return <Note tone="amber" className="mb12">偏好设置暂时存不上</Note>;
}

const readPermission = () => (typeof window === 'undefined' || !('Notification' in window) ? 'unsupported' : window.Notification.permission);

export function NotiSection({ prefs }) {
  const { loaded, error, save } = prefs;
  const n = prefs.prefs.notifications;
  const [perm, setPerm] = useState(readPermission);

  // 用户可能在浏览器设置里改了授权，回到页面时重读
  useEffect(() => {
    const on = () => setPerm(readPermission());
    window.addEventListener('focus', on);
    return () => window.removeEventListener('focus', on);
  }, []);

  const ask = async () => {
    try {
      const p = await window.Notification.requestPermission();
      setPerm(p);
      return p;
    } catch {
      return readPermission();
    }
  };

  const toggleSystem = async (on) => {
    if (!on) { save('notifications.system', false); return; }
    let p = readPermission();
    if (p === 'unsupported') { toast('这个浏览器不支持系统通知', 'alert'); return; }
    if (p === 'default') p = await ask();
    if (p !== 'granted') {
      toast(p === 'denied' ? '浏览器已禁止本站通知，请在浏览器设置里允许' : '没有拿到通知权限', 'alert');
      return;
    }
    save('notifications.system', true);
  };

  const pn = permissionNote(perm, n.system);
  const row = ([k, t]) => (
    <div className="pl-sw" key={k}>
      <div className={cx(!n.system && 'me-off')}><div className="sm b">{t}</div></div>
      <Toggle on={n[k]} disabled={!n.system} title={n.system ? undefined : '先打开系统通知'} onChange={(v) => save(`notifications.${k}`, v)} />
    </div>
  );

  return (
    <>
      <PageHead title="通知" />
      <LoadNote error={error} />
      {loaded ? (
        <div style={{ maxWidth: 640 }}>
          <Card title="系统通知" icon="bell" extra={<Toggle on={n.system} onChange={toggleSystem} />}>
            {pn ? (
              <Note tone={pn.tone} className="mb12">
                <div className="row">
                  <span className="grow">{pn.text}</span>
                  {pn.action ? <Button size="xs" kind="soft" onClick={ask}>{pn.action}</Button> : null}
                </div>
              </Note>
            ) : null}
            {NOTI_GROUPS.flatMap((g, i) => [
              <div key={g.title} className={cx('sec-t', !n.system && 'me-off')} style={i ? { marginTop: 12 } : undefined}>{g.title}</div>,
              ...g.items.map(row),
            ])}
          </Card>
        </div>
      ) : null}
    </>
  );
}
