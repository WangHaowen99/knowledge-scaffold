// 系统设置 · 会话与权限（SY-09）、脚本验证沙箱（SY-10、SY-11）。
// 会话池在这里只读：运维在运维管控里调整，覆盖环境变量默认值（契约 #56）。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Icon, Inp, KV, Note, Table, cx,
} from '../../ui/index.jsx';
import { LoadNote, Loading, SysHead, useLoad } from './parts.jsx';
import {
  PERMISSION_MODES, fmtDateTime, filterPackages, permissionModes, poolRows, sandboxLine,
} from './sysModel.js';

export function SessionSection() {
  const { api } = useApp();
  const { data, error, setData, reload } = useLoad(() => api.sessionSettings());
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  // 没给 allowed 时四种都能选
  const allowedModes = data?.allowed?.length ? data.allowed : PERMISSION_MODES.map(([k]) => k);
  const pick = async (mode) => {
    if (busy || mode === data.permission_mode || !allowedModes.includes(mode)) return;
    setBusy(true);
    try {
      setData(await api.saveSessionSettings({ permission_mode: mode }));
      setSaved(true);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <SysHead title="会话与权限" desc="助手调用工具前要不要问人。改动只对新会话生效。" />
      {!data ? (error ? <LoadNote error={error} what="会话与权限" onRetry={reload} /> : <Loading />) : (
        <>
          {saved ? <div className="mb12"><Note tone="teal" icon="check">已保存，新会话生效。</Note></div> : null}
          <Card title="权限模式" icon="shield">
            <div className="pl-radio">
              {permissionModes(data.pool?.permission_timeout_s).map(([k, t, c]) => {
                const on = data.permission_mode === k;
                const allowed = allowedModes.includes(k);
                return (
                  <div key={k} className={cx('opt', on && 'on', !allowed && 'sy-dis')} role="radio" aria-checked={on}
                    title={allowed ? undefined : '当前部署不允许这个模式'} onClick={() => pick(k)}>
                    <span className={cx('rad', on && 'on')} />
                    <div><div className="t">{t}</div><div className="c">{c}</div></div>
                  </div>
                );
              })}
            </div>
          </Card>
          <Card title="会话池" sub="只读 · 运维可在运维管控里调整" icon="server" className="mt16">
            <div className="grid3">
              {poolRows(data.pool).map((r) => (
                <div key={r.key} className="pl-kvro">
                  <Icon name="lock" size={14} />
                  <div className="grow"><div className="sm b">{r.label}</div><div className="xs muted mono">{r.sub}</div></div>
                  <b className="num">{r.value}</b>
                </div>
              ))}
            </div>
            <div className="row mt12">
              <span className="xs muted">超过上限时先驱逐最久空闲的会话；全部在忙就排队，并告诉用户排第几位。</span>
              <span className="sp" />
              <a className="link xs" onClick={() => navigate(pagePath('ops-ctl'))}>在运维管控查看 →</a>
            </div>
          </Card>
        </>
      )}
    </>
  );
}

export function SandboxSection() {
  const { api } = useApp();
  const { data, error, setData, reload } = useLoad(() => api.sandboxSettings());
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState('');

  const probe = async () => {
    setBusy(true);
    try {
      const v = await api.probeSandbox();
      setData(v);
      toast(v.connected ? '已连接脚本验证沙箱' : '仍然连不上脚本验证沙箱', v.connected ? 'check' : 'alert');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const pkgs = filterPackages(data?.packages, q);
  return (
    <>
      <SysHead
        title="脚本验证沙箱"
        desc="代码生成时，生成的脚本在这里实际跑一遍再提交。"
        actions={<Button icon="refresh" disabled={busy} onClick={probe}>{busy ? '检测中…' : '重新检测'}</Button>}
      />
      {!data ? (error ? <LoadNote error={error} what="脚本验证沙箱" onRetry={reload} /> : <Loading />) : (
        <>
          <div className="card pad row g16">
            <span className={cx('pl-ico', data.connected ? 'ind' : 'sy-bad')} style={{ width: 44, height: 44 }}>
              <Icon name={data.connected ? 'shieldok' : 'alert'} size={20} />
            </span>
            <div className="grow">
              <div className="b">{data.connected ? '已连接脚本验证沙箱' : '没有连上脚本验证沙箱'}</div>
              <div className="xs muted">{sandboxLine(data)}</div>
            </div>
            <KV w="96px" pairs={[['验证用 Python', data.python || '—'], ['脚本验证网关', data.gateway || '—'], ['检测时间', fmtDateTime(data.checked_at)]]} />
          </div>
          <div className="mt12">
            {data.isolation?.network === 'denied' || !data.isolation
              ? <Note tone="amber" icon="globe">沙箱禁止出网，查数脚本请用合成输入验证。</Note>
              : <Note tone="red" icon="globe">沙箱当前允许出网，请检查脚本验证网关下发的网络策略。</Note>}
          </div>
          <div className="row mt16 mb12">
            <div className="sec-t" style={{ margin: 0 }}>沙箱里的 Python 包</div>
            <span className="sp" />
            <Inp icon="search" placeholder="按包名筛选" w="240px" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          {data.packages?.length ? (
            <Table
              compact
              cols={['包名', '版本', '用途']}
              rows={pkgs.map((p) => [
                <span className="mono">{p.name}</span>,
                p.version ? <span className="mono">{p.version}</span> : <span className="muted">未安装（按包元数据列出）</span>,
                <span className="muted sm">{p.purpose || '—'}</span>,
              ])}
            />
          ) : <div className="card pad sm muted">{data.connected ? '沙箱没有报告 Python 包。' : '连上沙箱后才能列出里面的 Python 包。'}</div>}
          {data.packages?.length && !pkgs.length ? <div className="sm muted mt8">没有匹配「{q}」的包。</div> : null}
        </>
      )}
    </>
  );
}
