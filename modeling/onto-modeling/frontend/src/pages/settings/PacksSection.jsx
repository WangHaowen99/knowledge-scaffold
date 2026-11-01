// 项目设置 · 能力与插件（PL-05～PL-11）：锁定版本、升级、回滚、停用、启用、撤销待生效的切换；
// 停用带页面的能力后左栏页面消失（外壳收到 extensions.updated 重拉 PL-12），待生效 chip 跟着 assembly.updated 刷新
import { useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Chip, Icon, Menu, Note, Table, useOutside,
} from '../../ui/index.jsx';
import {
  CATEGORY_LABEL, pendingSummary, rowActions, statusChip, switchToast, uiCell,
} from './settingsModel.js';

const UI_ICON = { page: 'layout', kernel: 'layout', subpages: 'folder' };

function UiCell({ ui }) {
  const c = uiCell(ui);
  if (c.kind === 'none') return <span className="evd-ui no">{c.text}</span>;
  return (
    <span className={c.kind === 'kernel' ? 'evd-ui' : 'evd-ui pg'} data-pop={c.pop}>
      <Icon name={UI_ICON[c.kind]} size={12} />{c.text}
    </span>
  );
}

function MoreMenu({ items }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  return (
    <span className="ps-more" ref={ref}>
      <Button size="xs" icon="more" kind="ghost" title="更多" onClick={() => setOpen((v) => !v)} />
      {open ? <Menu items={items} onPick={() => setOpen(false)} /> : null}
    </span>
  );
}

export function PacksSection({ d, reload, archived }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [busy, setBusy] = useState(null);
  const items = d.packs?.items || [];
  const assembly = d.assembly;
  const summary = pendingSummary(assembly?.pending_changes, items);

  const run = async (op, p) => {
    if (archived) { toast('项目已归档，只能查看', 'lock'); return; }
    setBusy(`${op}:${p.name}`);
    try {
      let res;
      if (op === 'enable') res = await api.enablePack(pid, { name: p.name });
      else if (op === 'upgrade') res = await api.upgradePack(pid, p.name, { to_version: p.latest_available });
      else if (op === 'rollback') res = await api.rollbackPack(pid, p.name, { to_version: p.rollback_to });
      else if (op === 'disable') res = await api.disablePack(pid, p.name);
      else if (op === 'cancel_pending') res = await api.cancelPackPending(pid, p.name);
      toast(switchToast(op, p, res), op === 'cancel_pending' ? 'undo' : 'check');
    } catch (e) {
      const checks = e.details?.checks?.filter((c) => !c.ok).map((c) => c.label).join('、');
      toast(checks ? `${errorText(e)}：${checks}` : errorText(e), 'alert');
    } finally {
      setBusy(null);
      reload('packs', 'assembly');
    }
  };

  const rows = items.map((p) => {
    const st = statusChip(p);
    const acts = rowActions(p);
    return [
      <div key="n"><b>{p.label}</b><div className="xs faint">{CATEGORY_LABEL[p.category] || p.category}</div></div>,
      <UiCell key="u" ui={p.ui} />,
      <span key="l" className="mono b" style={{ fontSize: 12 }}>{p.locked_version}</span>,
      p.latest_available && p.latest_available !== p.locked_version
        ? <span key="a" className="mono" style={{ fontSize: 12, color: 'var(--blue2)' }}>{p.latest_available}</span>
        : <span key="a" className="faint sm">已是最新</span>,
      <Chip key="s" tone={st.tone} sm icon={st.icon} data-pop={st.pop}>{st.text}</Chip>,
      <div key="x" className="row g4">
        {acts.note ? <span className="xs faint">{acts.note}</span> : null}
        {acts.buttons.map((b) => (
          <Button key={b.op} size="xs" kind={b.kind} icon={b.icon} disabled={!!busy} onClick={() => run(b.op, p)}>{b.label}</Button>
        ))}
        {acts.more.length ? (
          <MoreMenu
            items={[
              ...acts.more.map((b) => ({ label: b.label, icon: b.op === 'disable' ? 'ban' : undefined, onClick: () => run(b.op, p) })),
              { label: '插件详情', icon: 'info', onClick: () => navigate(pagePath('plugin-detail'), { name: p.name }) },
            ]}
          />
        ) : null}
      </div>,
    ];
  });

  return (
    <div className="card" data-sec="plugins">
      <div className="card-h">
        <h3>能力与插件</h3><span className="sub">本项目用的版本固定下来，升级由你决定</span><span className="sp" />
        <Button size="sm" kind="primary" icon="plus" onClick={() => navigate(pagePath('plugins'))}>添加能力</Button>
      </div>
      <div className="card-b">
        {summary ? <div className="row mb12 wrap" style={{ gap: 8 }}><Chip tone="amber" icon="clock">{summary}</Chip></div> : null}
        {assembly?.state === 'failed' && assembly.last_error ? (
          <Note tone="red" className="mb12">装配失败，本项目保持原来的版本：{assembly.last_error.message}（错误编号 {assembly.last_error.error_id}）</Note>
        ) : null}
        {d.packs ? <Table compact cols={['能力', '界面', '锁定版本', '最新可用', '状态', '']} rows={rows} /> : <Note tone="red">本项目的能力没有读出来，刷新页面再试。</Note>}
        <div className="mt12"><Note tone="blue" icon="info">升级从下一条消息开始生效，正在进行的一轮按旧版本跑完。包之间不兼容时拒绝切换，不会切到一半；装配失败只影响这个项目，并保持原来的版本。</Note></div>
        <div className="mt8"><Note tone="gray" icon="layout">停用带页面的能力，对应页面会从左栏消失；已经产生的内容留在知识包里，重新启用后回来。</Note></div>
      </div>
    </div>
  );
}
