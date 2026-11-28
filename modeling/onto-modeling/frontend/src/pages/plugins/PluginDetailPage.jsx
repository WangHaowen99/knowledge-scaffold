// 插件详情（设计稿 plugin-detail）：地址参数 ?pack=<包名>（可带 &project=）。
// PL-02 详情（带「启用到」的项目）、PL-05 待生效的是什么；英雄区下拉菜单：停用 PL-09（先确认）、撤销切换 PL-10、只在下次自主构建时启用新版本 PL-07、去项目设置；
// 「更新到 x」先 PL-03 兼容检查，不通过弹「暂时不能更新」，通过则 PL-07（纯数据包先看更新弹窗）。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, packActionErrorText } from '../../api/errors.js';
import { ShellG } from '../../shell/Shell.jsx';
import { can } from '../../state/roles.js';
import {
  Button, Chip, Empty, Icon, Menu, Modal, Note, Stat, Tabs, useOutside,
} from '../../ui/index.jsx';
import { UiChip } from './catalogCards.jsx';
import { usePackActions } from './catalogDialogs.jsx';
import { PACK_CATEGORIES, packIcon, publisherChip, trustChip, versionOf } from './catalogModel.js';
import {
  contentGroups, contentTotal, converterDefaults, disableConfirm, flowCaption, flowLayout, heroActions, heroFacts, heroMenu, statCards,
  untrustConfirm,
} from './detailModel.js';
import { Changelog, ContentsGrid, FlowMap } from './detailParts.jsx';
import { usePluginProject } from './parts.jsx';
import '../../styles/plugins.css';

function NoPack({ title, children }) {
  return (
    <ShellG active="plugins">
      <div className="card" style={{ padding: 30, maxWidth: 640, margin: '40px auto' }}>
        <Empty icon="puzzle" title={title} actions={<Button icon="arrowl" onClick={() => navigate(pagePath('plugins'))}>去插件中心</Button>}>{children || null}</Empty>
      </div>
    </ShellG>
  );
}

// 转换器的默认（PL-20 / PL-21）：同一种文件有别的转换器时才出现
function ConverterDefaults({ d, admin }) {
  const { api } = useApp();
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    if (d?.converter) api.converters().then((r) => alive && setData(r)).catch(() => {});
    return () => { alive = false; };
  }, [api, d?.name, d?.converter]);
  const rows = converterDefaults(d, data, { admin });
  if (!rows.length) return null;
  const setDefault = async (row) => {
    setBusy(true);
    try {
      setData(await api.setConverterDefault({ type: row.type, converter: d.name }));
      toast(`${row.label} 以后用「${d.label}」转换`, 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="row wrap g4 mt8">
      {rows.map((row) => (row.isDefault
        ? <Chip key={row.type} tone="teal" icon="check">{row.label} 默认</Chip>
        : row.canSet ? <Button key={row.type} size="xs" kind="soft" disabled={busy} onClick={() => setDefault(row)}>设为 {row.label} 默认</Button> : null))}
    </div>
  );
}

const disableToast = (d, res) => {
  const page = d.ui?.kind === 'page' ? d.ui.pages.map((x) => `「${x.title}」`).join('') : '';
  return `已停用「${d.label}」${page ? `，左栏的${page}已移除` : ''}${res?.effective === 'immediate' ? '' : '，下一条消息生效'}`;
};

export default function PluginDetailPage({ route }) {
  const { api, user } = useApp();
  const name = route.query.pack || '';
  const { projectId, projects, ready } = usePluginProject(route);
  const [d, setD] = useState(null);
  const [failure, setFailure] = useState(null);
  const [pending, setPending] = useState(null);
  const [tab, setTab] = useState('ov');
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmOff, setConfirmOff] = useState(false);
  const [confirmUntrust, setConfirmUntrust] = useState(false);
  const [trustBusy, setTrustBusy] = useState(false);
  const admin = can(user, 'admin');
  const menuRef = useRef(null);
  useOutside(menuRef, () => setMenuOpen(false), menuOpen);
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!name || !ready) return;
    const n = ++seq.current;
    try {
      const [detail, pp] = await Promise.all([
        api.pack(name, projectId ? { project_id: projectId } : undefined),
        projectId ? api.projectPacks(projectId).catch(() => null) : null,
      ]);
      if (n !== seq.current) return;
      setD(detail);
      setFailure(null);
      setPending(pp?.items?.find((p) => p.name === name)?.pending || null);
    } catch (e) {
      if (n === seq.current) setFailure(e);
    }
  }, [api, name, projectId, ready]);
  useEffect(() => { setD(null); setFailure(null); setTab('ov'); }, [name]);
  useEffect(() => { load(); }, [load]);

  const projectName = projects.find((p) => p.id === projectId)?.name;
  const actions = usePackActions({ projectId, projectName, onChanged: load });

  if (!name) return <NoPack title="没有指定插件" />;
  if (failure?.code === 'pack_not_found') return <NoPack title="找不到这个插件">插件目录里没有「{name}」</NoPack>;

  const pickMenu = async (key) => {
    setMenuOpen(false);
    if (key === 'settings') { navigate(pagePath('proj-settings', projectId), { section: 'plugins' }); return; }
    if (key === 'next_autobuild') { actions.act('update', d, { effective: 'next_autobuild' }); return; }
    if (key === 'disable') { setConfirmOff(true); return; }
    if (key === 'cancel_pending') {
      try {
        await api.cancelPackPending(projectId, d.name);
        toast(`已撤销，保持 ${pending?.from || d.locked_version || d.version}`, 'undo');
        load();
      } catch (e) { toast(errorText(e), 'alert'); }
    }
  };
  // 多源连接器的信任（PL-28）与撤销（PL-29，先确认）
  const setTrust = async (on) => {
    setTrustBusy(true);
    try {
      const version = d.version || d.latest;
      if (on) await api.trustPack(d.name, version); else await api.untrustPack(d.name, version);
      setConfirmUntrust(false);
      toast(on ? `已信任「${d.label}」` : `已撤销信任「${d.label}」`, 'check');
      load();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setTrustBusy(false);
    }
  };
  const heroAct = (action) => {
    if (action === 'trust') setTrust(true);
    else if (action === 'untrust') setConfirmUntrust(true);
    else if (action) actions.act(action, d);
  };

  const disable = async () => {
    try {
      const res = await api.disablePack(projectId, d.name);
      setConfirmOff(false);
      toast(disableToast(d, res), 'check');
      load();
    } catch (e) { toast(packActionErrorText(e), 'alert'); }
  };

  let body;
  if (!d) {
    body = failure
      ? <Note tone="red">插件详情没有读出来：{errorText(failure)} <Button size="xs" onClick={load}>重试</Button></Note>
      : <div className="row sm muted"><span className="spin" />正在读取插件详情…</div>;
  } else {
    const cat = PACK_CATEGORIES[d.category] || [d.category, 'puzzle'];
    const pub = publisherChip(d);
    const off = disableConfirm(d);
    const hero = heroActions(d, { pending, hasProject: !!projectId, admin });
    const trust = trustChip(d);
    const untrust = untrustConfirm(d);
    const kernel = d.ui?.kind === 'kernel';
    const groups = contentGroups(d);
    const stats = statCards(d, { hasProject: !!projectId });
    const insertions = d.flow_insertions || [];
    const caption = flowCaption(d, kernel);
    body = (
      <>
        <div className="crumb mb12">
          <a onClick={() => navigate(pagePath('plugins'))} style={{ color: 'var(--ink3)', cursor: 'pointer' }}>插件中心</a><span className="sep">/</span>
          <a onClick={() => navigate(pagePath('plugins'), { cat: d.category })} style={{ color: 'var(--ink3)', cursor: 'pointer' }}>{cat[0]}</a><span className="sep">/</span>
          <b>{d.label}</b>
        </div>
        <div className="card">
          <div className="pl-hero">
            <span className="big"><Icon name={packIcon(d)} size={24} /></span>
            <div className="grow">
              <h1>{d.label} <span style={{ fontSize: 13, color: 'var(--ink3)', fontWeight: 500 }}>{versionOf(d)}</span></h1>
              <div className="row wrap g4 mt4">
                {pub ? <Chip tone={pub.tone}>{pub.text}</Chip> : null}
                {trust ? <Chip tone={trust.tone} icon={trust.icon}>{trust.text}</Chip> : null}
                {d.badges?.includes('recommended') ? <Chip tone="teal" icon="star">推荐</Chip> : null}
                {d.badges?.includes('new') ? <span className="badge-new">新</span> : null}
                {d.category === 'capability' ? <UiChip ui={d.ui} sm={false} /> : null}
              </div>
              <div className="muted mt8" style={{ maxWidth: 640 }}>{d.description}</div>
              <ConverterDefaults d={d} admin={admin} />
              <div className="row g16 mt12 sm muted">
                {heroFacts(d).map(([k, v]) => <span key={k}>{k} <b style={{ color: 'var(--ink2)' }}>{v}</b></span>)}
              </div>
            </div>
            <div className="col" style={{ alignItems: 'flex-end', gap: 10, flex: 'none' }}>
              <div className="row">
                {hero.menu ? (
                  <span className="pd-menu" ref={menuRef}>
                    <Button kind="soft" icon={hero.menu.icon} title={projectName ? `本项目：${projectName}` : undefined} onClick={() => setMenuOpen((v) => !v)}>{hero.menu.label}<Icon name="chevd" size={12} /></Button>
                    {menuOpen ? (
                      <Menu items={heroMenu(d, { pending }).map((x) => (x === '-' ? x : { ...x, onClick: () => pickMenu(x.key) }))} />
                    ) : null}
                  </span>
                ) : null}
                {hero.primary ? (
                  <Button
                    kind={hero.primary.kind ?? 'primary'}
                    icon={hero.primary.icon}
                    disabled={hero.primary.disabled || actions.busy === d.name || trustBusy}
                    title={hero.primary.title}
                    onClick={() => heroAct(hero.primary.action)}
                  >
                    {hero.primary.label}
                  </Button>
                ) : null}
              </div>
              {hero.hint ? <div className="xs muted" style={{ maxWidth: 340, textAlign: 'right' }}>{hero.hint}</div> : null}
            </div>
          </div>
        </div>

        <div className="card mt16">
          <div style={{ padding: '0 16px' }}>
            <Tabs items={[['ov', '概览'], ['ct', '包含什么', contentTotal(groups)], ['ver', '更新记录', d.changelog?.length || 0]]} value={tab} onChange={setTab} />
          </div>
          {tab === 'ov' ? (
            <div className="card-b" style={{ padding: '18px 18px 20px' }}>
              <div className="sec-t"><Icon name="branch" size={14} />{kernel ? '它定义的主线' : '它往流程里加了什么'}</div>
              <FlowMap layout={flowLayout(insertions, { kernel, caption })} caption={caption} />
              <div className="grid2 mt16">
                {stats.map((s) => <Stat key={s.label} label={s.label} value={s.value} unit={s.unit} desc={s.desc} />)}
              </div>
            </div>
          ) : null}
          {tab === 'ct' ? <div className="card-b" style={{ padding: 18 }}><ContentsGrid groups={groups} /></div> : null}
          {tab === 'ver' ? <div className="card-b" style={{ padding: '8px 18px 14px' }}><Changelog changelog={d.changelog} /></div> : null}
        </div>

        <Modal
          open={confirmOff}
          onClose={() => setConfirmOff(false)}
          icon="ban"
          danger
          title={off.title}
          w={440}
          foot={<><Button onClick={() => setConfirmOff(false)}>取消</Button><Button kind="danger" className="fill" onClick={disable}>停用</Button></>}
        >
          <div className="sm muted">{off.body}</div>
        </Modal>
        <Modal
          open={confirmUntrust}
          onClose={() => setConfirmUntrust(false)}
          icon="ban"
          danger
          title={untrust.title}
          w={440}
          foot={<><Button onClick={() => setConfirmUntrust(false)}>取消</Button><Button kind="danger" className="fill" disabled={trustBusy} onClick={() => setTrust(false)}>撤销信任</Button></>}
        >
          <div className="sm muted">{untrust.body}</div>
        </Modal>
      </>
    );
  }

  return (
    <ShellG active="plugins">
      {body}
      {actions.dialogs}
    </ShellG>
  );
}
