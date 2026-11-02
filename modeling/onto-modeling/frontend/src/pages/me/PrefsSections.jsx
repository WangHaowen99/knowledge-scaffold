// 个人设置 · 外观、通知：同一份偏好（A-08，PUT 整体替换），点了就存，失败退回上次存好的值。
// 系统通知要浏览器授权：打开总开关前先查 Notification 权限，没授权就先请求，拿不到不打开。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Button, Card, Icon, Note, Radio, Toggle, cx } from '../../ui/index.jsx';
import { DEFAULT_PREFS, NOTI_GROUPS, NOTI_PREVIEW, normalizePrefs, permissionNote, withPref } from './meModel.js';

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
  // 404：服务端还没有 A-08（B2 后端现状，gaps B2 第 12 条）
  const text = error.status === 404 ? '服务端还没有提供偏好设置，先按默认显示，改动暂时存不上。' : `偏好暂时读不到（${errorText(error)}），先按默认显示，改动可能存不上。`;
  return <Note tone="amber" className="mb12">{text}</Note>;
}

const THEME_CARDS = [
  ['light', '浅色', '#F3F6FB', '#fff', '#0B1A33'],
  ['dark', '深色', '#0D1526', '#16213A', '#E6EEFB'],
];

export function LookSection({ prefs }) {
  const { loaded, error, save } = prefs;
  const theme = prefs.prefs.theme;
  return (
    <>
      <PageHead eyebrow="个人设置" title="外观" desc="只影响你自己的界面。" />
      <LoadNote error={error} />
      {loaded ? (
        <div style={{ maxWidth: 640 }}>
          <Card title="主题" icon="sun">
            <div className="row g16">
              {THEME_CARDS.map(([k, t, bg, pn, tx]) => (
                <div key={k} className={cx('pl-theme', theme === k && 'on')} onClick={() => theme !== k && save('theme', k)} role="radio" aria-checked={theme === k}>
                  <div className="pv" style={{ background: bg }}>
                    <div style={{ background: pn, borderRadius: 6 }} />
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                      <div style={{ height: 16, background: pn, borderRadius: 5 }} />
                      <div style={{ flex: 1, background: pn, borderRadius: 6, padding: 6 }}>
                        <div style={{ width: '60%', height: 5, borderRadius: 3, background: tx, opacity: 0.5 }} />
                        <div style={{ width: '40%', height: 5, borderRadius: 3, background: '#2563EB', marginTop: 5 }} />
                      </div>
                    </div>
                  </div>
                  <div className="row mt8"><Radio on={theme === k} /><b className="sm">{t}</b></div>
                </div>
              ))}
            </div>
            {theme === 'dark' ? <Note tone="gray" className="mt12">深色界面还在制作，选好的偏好已经存下，在那之前仍按浅色显示。</Note> : null}
          </Card>
        </div>
      ) : null}
    </>
  );
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
      toast(p === 'denied' ? '浏览器已禁止本站发通知，请在浏览器的网站设置里允许后再打开' : '没有拿到通知权限，系统通知没有打开', 'alert');
      return;
    }
    save('notifications.system', true);
  };

  const pn = permissionNote(perm, n.system);
  const row = ([k, t, d]) => (
    <div className="pl-sw" key={k}>
      <div className={cx(!n.system && 'me-off')}><div className="sm b">{t}</div><div className="xs muted">{d}</div></div>
      <Toggle on={n[k]} disabled={!n.system} title={n.system ? undefined : '先打开系统通知'} onChange={(v) => save(`notifications.${k}`, v)} />
    </div>
  );

  return (
    <>
      <PageHead eyebrow="个人设置" title="通知" desc="你不在工作台页面时，用系统通知提醒你。" />
      <LoadNote error={error} />
      {loaded ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 340px', gap: 16 }}>
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
            <div className="mt12"><Note tone="gray" icon="info">标题和图标闪烁始终开启：关掉系统通知后，标签页的标题和图标仍会闪烁提醒你。</Note></div>
          </Card>
          <div className="col" style={{ gap: 12 }}>
            <div className="sec-t">预览</div>
            {NOTI_PREVIEW.map((p) => (
              <div key={p.title} className={cx('pl-noti', !(n.system && n[p.pref]) && 'me-off')}>
                <div className="logo" style={{ width: 34, height: 34 }}>本</div>
                <div className="grow">
                  <div className="row"><b className="sm">本体建模工作台</b><span className="sp" /><span className="xs muted">{p.when}</span></div>
                  <div className="sm b mt4">{p.title}</div>
                  <div className="xs muted">{p.body}</div>
                </div>
              </div>
            ))}
            <div className="card pad sm">
              <div className="b mb8">标签页标题闪烁</div>
              <div className="row g4"><span className="chip line"><Icon name="bell" size={12} />(1) 需要你决定 · 本体建模…</span></div>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
