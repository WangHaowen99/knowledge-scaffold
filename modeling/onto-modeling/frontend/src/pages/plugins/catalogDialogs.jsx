// 启用、更新、平台安装的流程与弹窗（插件中心、插件详情共用）：
// 启用：PL-03 兼容检查 → 启用弹窗（检查、会带来什么、界面、生效时机）→ PL-06；
// 更新：PL-03 → 不通过弹「暂时不能更新」；纯数据包弹「更新 x → y」；其余直接 PL-07；
// 提供方：管理员 PL-19 安装到平台，其他人 PL-04 申请。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { can } from '../../state/roles.js';
import {
  Button, Chip, Modal, Note,
} from '../../ui/index.jsx';
import { PackIcon, UiChip } from './catalogCards.jsx';
import {
  PACK_CATEGORIES, blockedView, bringsView, compatRows, effectiveNote, enableToast, impactText, isDataPack, uiEffectText, updateRoute, updateToast,
} from './catalogModel.js';

function Brings({ brings }) {
  const v = bringsView(brings);
  if (v.list) {
    return <ul className="sm" style={{ paddingLeft: 18, color: 'var(--ink2)', lineHeight: 1.8 }}>{v.list.map((x) => <li key={x}>{x}</li>)}</ul>;
  }
  return <div className="sm muted">{v.text}</div>;
}

function EnableDialog({ dlg, projectName, busy, onOk, onClose }) {
  const { pack: p, compat, error } = dlg;
  const blocked = compat && !compat.ok ? blockedView(compat) : null;
  return (
    <Modal
      open
      onClose={onClose}
      icon="puzzle"
      title={`启用到「${projectName || '本项目'}」`}
      sub="启用后保存为一个版本"
      w={560}
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" icon="check" disabled={!compat?.ok || busy} onClick={onOk}>启用</Button>
        </>
      )}
    >
      <div className="row g12 mb12">
        <PackIcon category={p.category} />
        <div><div className="b">{p.label}</div><div className="mono xs muted">{p.name}@{p.latest || p.version}</div></div>
      </div>
      <div className="sec-t">兼容检查</div>
      {!compat && !error ? <div className="row sm muted"><span className="spin" />正在检查…</div> : null}
      {error ? <Note tone="red">兼容检查没做成：{error}</Note> : null}
      {compat ? (
        <div className="col" style={{ gap: 6 }}>
          {compatRows(compat.checks, p.ui).map((c) => (
            <div className="row sm" key={c.id}>
              <Chip tone={c.ok ? 'teal' : 'red'} sm icon={c.ok ? 'check' : 'x'}>{c.ok ? '通过' : '不通过'}</Chip>
              <span>{c.label}</span><span className="sp" /><span className="muted xs">{c.detail}</span>
            </div>
          ))}
        </div>
      ) : null}
      {blocked ? <Note tone="red" className="mt12">暂时不能启用：{blocked.reason}。条件不满足时不会启用到一半。</Note> : null}
      <div className="sec-t" style={{ marginTop: 16 }}>会带来什么</div>
      {compat ? <Brings brings={compat.brings} /> : <div className="sm muted">{p.description}</div>}
      <div className="sec-t" style={{ marginTop: 16 }}>界面</div>
      <div className="row g8 sm"><UiChip ui={p.ui} fallback /><span className="muted">{uiEffectText(compat?.ui_effect, p.ui)}</span></div>
      {p.category === 'capability' ? <div className="xs muted mt4">带页面的能力启用后，项目左栏「插件」分组会多出对应页面；停用后页面消失，已经产生的内容留在知识包里。</div> : null}
      <div className="mt12">
        <Note tone="gray" icon="clock"><b>什么时候生效：</b>{effectiveNote(compat?.effective || (isDataPack(p) ? 'immediate' : 'next_message'))}</Note>
      </div>
    </Modal>
  );
}

function ConfirmUpdateDialog({ dlg, busy, onOk, onClose }) {
  const { pack: p, compat, to } = dlg;
  const from = p.locked_version || p.version;
  return (
    <Modal
      open
      onClose={onClose}
      icon="refresh"
      title={`更新 ${p.name} ${from} → ${to}`}
      sub={`${PACK_CATEGORIES[p.category]?.[0] || ''} · ${compat.effective === 'immediate' ? '现读即生效' : '下一条消息生效'}`}
      w={560}
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" icon="refresh" disabled={busy} onClick={onOk}>更新</Button>
        </>
      )}
    >
      <div className="sec-t">这个版本带来</div>
      <Brings brings={compat.brings} />
      <div className="sec-t" style={{ marginTop: 12 }}>对已有产物的影响</div>
      <Note tone="teal" icon="shieldok">{impactText(compat.impact)}</Note>
      <div className="mt12"><Note tone="gray" icon="undo">回滚：在项目设置里改回 {from} 即可，旧版本一直保留。</Note></div>
    </Modal>
  );
}

function BlockedDialog({ dlg, onClose }) {
  const { pack: p, compat, to } = dlg;
  const v = blockedView(compat);
  return (
    <Modal
      open
      onClose={onClose}
      icon="alert"
      danger
      title={`暂时不能更新到 ${to}`}
      sub={`本项目继续用 ${p.locked_version || p.version}`}
      w={520}
      foot={<Button kind="primary" onClick={onClose}>知道了</Button>}
    >
      <Note tone="red">暂时不能更新：{v.reason}。条件不满足时不会更新到一半。</Note>
      {v.details.map((t) => <div className="sm muted mt12" key={t}>{t}</div>)}
    </Modal>
  );
}

/**
 * 启用、更新、平台安装。onChanged 在切换成功后调（页面重取 PL-01 / PL-02）。
 * 返回 { act(action, pack, opts), busy, requested, dialogs }；action 为 enable | update | install。
 */
export function usePackActions({ projectId, projectName, onChanged }) {
  const { api, user } = useApp();
  const [dlg, setDlg] = useState(null);
  const [busy, setBusy] = useState(null);
  const [requested, setRequested] = useState(() => new Set());
  const close = () => setDlg(null);

  const compatOf = (p, version) => api.packCompatibility(p.name, version, { project_id: projectId });
  const failed = (e, p, to) => {
    if (e?.code === 'pack_incompatible' && e.details?.checks) {
      setDlg({ kind: 'blocked', pack: p, to, compat: { ok: false, checks: e.details.checks, blocked_reason: null } });
    } else toast(errorText(e), 'alert');
  };

  async function enable(p) {
    if (!projectId) { toast('先选一个项目', 'info'); return; }
    setDlg({ kind: 'enable', pack: p, compat: null, error: null });
    try {
      const compat = await compatOf(p, p.latest || p.version);
      setDlg((d) => (d?.kind === 'enable' && d.pack.name === p.name ? { ...d, compat } : d));
    } catch (e) {
      setDlg((d) => (d?.kind === 'enable' && d.pack.name === p.name ? { ...d, error: errorText(e) } : d));
    }
  }
  async function confirmEnable() {
    const p = dlg.pack;
    setBusy(p.name);
    try {
      const res = await api.enablePack(projectId, { name: p.name, version: p.latest || p.version });
      toast(enableToast(p, res), 'check');
      close();
      onChanged?.();
    } catch (e) {
      if (e?.code === 'pack_incompatible' && e.details?.checks) setDlg((d) => ({ ...d, compat: { ...(d.compat || {}), ok: false, checks: e.details.checks } }));
      else toast(errorText(e), 'alert');
    } finally { setBusy(null); }
  }

  async function upgrade(p, to, { viaDialog = false, effective } = {}) {
    setBusy(p.name);
    try {
      const res = await api.upgradePack(projectId, p.name, effective ? { to_version: to, effective } : { to_version: to });
      toast(effective === 'next_autobuild' ? `已排好：下次自主构建时启用${p.label} ${to}` : updateToast(p, to, res, viaDialog), 'refresh');
      close();
      onChanged?.();
    } catch (e) { failed(e, p, to); } finally { setBusy(null); }
  }

  async function update(p, { effective } = {}) {
    if (!projectId) { toast('先选一个项目', 'info'); return; }
    const to = p.latest;
    setBusy(p.name);
    let compat;
    try {
      compat = await compatOf(p, to);
    } catch (e) {
      toast(errorText(e), 'alert');
      setBusy(null);
      return;
    }
    setBusy(null);
    const route = updateRoute(p, compat);
    if (route === 'blocked') setDlg({ kind: 'blocked', pack: p, compat, to });
    else if (route === 'confirm' && !effective) setDlg({ kind: 'confirm', pack: p, compat, to });
    else await upgrade(p, to, { effective });
  }

  async function install(p) {
    setBusy(p.name);
    try {
      if (can(user, 'admin')) {
        await api.installPack(p.name, { version: p.latest || p.version });
        toast(`已开始安装「${p.label}」到平台，装好后所有项目都能登记这类数据源`, 'download');
        onChanged?.();
      } else {
        await api.requestPackInstall(p.name, { version: p.latest || p.version });
        setRequested((s) => new Set(s).add(p.name));
        toast('需要管理员权限，已发送安装申请', 'check');
      }
    } catch (e) { toast(errorText(e), 'alert'); } finally { setBusy(null); }
  }

  const act = (action, p, opts) => {
    if (action === 'enable') return enable(p);
    if (action === 'update') return update(p, opts);
    if (action === 'install') return install(p);
    return null;
  };

  let dialogs = null;
  if (dlg?.kind === 'enable') dialogs = <EnableDialog dlg={dlg} projectName={projectName} busy={!!busy} onOk={confirmEnable} onClose={close} />;
  else if (dlg?.kind === 'confirm') dialogs = <ConfirmUpdateDialog dlg={dlg} busy={!!busy} onOk={() => upgrade(dlg.pack, dlg.to, { viaDialog: true })} onClose={close} />;
  else if (dlg?.kind === 'blocked') dialogs = <BlockedDialog dlg={dlg} onClose={close} />;
  return { act, busy, requested, dialogs };
}
