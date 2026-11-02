// 插件详情（设计稿 plugin-detail）：地址参数 ?pack=<包名>（可带 &project=）。
// PL-02 详情（带「启用到」的项目）、PL-05 待生效的是什么；英雄区菜单：停用 PL-09、只在下次自主构建时启用新版本 PL-07、去项目设置；
// 「更新到 x」先 PL-03 兼容检查，不通过弹「暂时不能更新」，通过则 PL-07（纯数据包先看更新弹窗）。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { ShellG } from '../../shell/Shell.jsx';
import { can } from '../../state/roles.js';
import {
  Button, Chip, Empty, Icon, Menu, Modal, Note, Stat, Tabs,
} from '../../ui/index.jsx';
import { UiChip } from './catalogCards.jsx';
import { usePackActions } from './catalogDialogs.jsx';
import { PACK_CATEGORIES, packIcon, publisherChip, versionOf } from './catalogModel.js';
import {
  contentGroups, contentTotal, flowCaption, flowLayout, heroActions, heroFacts, heroMenu, heroMenuSub, statCards,
} from './detailModel.js';
import { Changelog, ContentsGrid, FlowMap } from './detailParts.jsx';
import { usePluginProject } from './parts.jsx';
import '../../styles/plugins.css';

function NoPack({ title, children }) {
  return (
    <ShellG active="plugins">
      <div className="card" style={{ padding: 30, maxWidth: 640, margin: '40px auto' }}>
        <Empty icon="puzzle" title={title} actions={<Button icon="arrowl" onClick={() => navigate(pagePath('plugins'))}>去插件中心</Button>}>{children}</Empty>
      </div>
    </ShellG>
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

  if (!name) return <NoPack title="没有指定插件">从插件中心选一个插件，查看它往流程里加了什么、包含什么、更新记录。</NoPack>;
  if (failure?.code === 'pack_not_found') return <NoPack title="找不到这个插件">插件目录里没有「{name}」，可能已经下架，或者地址写错了。</NoPack>;

  const pickMenu = async (key) => {
    setMenuOpen(false);
    if (key === 'settings') { navigate(pagePath('proj-settings', projectId), { section: 'plugins' }); return; }
    if (key === 'next_autobuild') { actions.act('update', d, { effective: 'next_autobuild' }); return; }
    if (key === 'disable') {
      try {
        const res = await api.disablePack(projectId, d.name);
        toast(disableToast(d, res), 'check');
        load();
      } catch (e) { toast(errorText(e), 'alert'); }
    }
  };

  let body;
  if (!d) {
    body = failure
      ? <Note tone="red">插件详情没有读出来：{errorText(failure)} <Button size="xs" onClick={load}>重试</Button></Note>
      : <div className="row sm muted"><span className="spin" />正在读取插件详情…</div>;
  } else {
    const cat = PACK_CATEGORIES[d.category] || [d.category, 'puzzle'];
    const pub = publisherChip(d);
    const hero = heroActions(d, { pending, hasProject: !!projectId, admin: can(user, 'admin'), requested: actions.requested.has(d.name) });
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
                <Chip tone="line" icon={cat[1]}>{cat[0]}</Chip>
                <Chip tone={pub.tone}>{pub.text}</Chip>
                {d.badges?.includes('recommended') ? <Chip tone="teal" icon="star">推荐</Chip> : null}
                {d.badges?.includes('new') ? <span className="badge-new">新</span> : null}
                {d.category === 'capability' ? <UiChip ui={d.ui} sm={false} /> : null}
              </div>
              <div className="muted mt8" style={{ maxWidth: 640 }}>{d.description}</div>
              <div className="row g16 mt12 sm muted">
                {heroFacts(d).map(([k, v]) => <span key={k}>{k} <b style={{ color: 'var(--ink2)' }}>{v}</b></span>)}
              </div>
            </div>
            <div className="col" style={{ alignItems: 'flex-end', gap: 10, flex: 'none' }}>
              <div className="row">
                {hero.menu ? (
                  <Button kind="soft" icon={hero.menu.icon} title={projectName ? `本项目：${projectName}` : undefined} onClick={() => setMenuOpen(true)}>{hero.menu.label}</Button>
                ) : null}
                {hero.primary ? (
                  <Button
                    kind={hero.primary.kind ?? 'primary'}
                    icon={hero.primary.icon}
                    disabled={hero.primary.disabled || actions.busy === d.name}
                    title={hero.primary.title}
                    onClick={() => hero.primary.action && actions.act(hero.primary.action, d)}
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
              <div className="grid3 mt16">
                {stats.map((s) => <Stat key={s.label} label={s.label} value={s.value} unit={s.unit} desc={s.desc} />)}
              </div>
              {insertions.length ? (
                <div className="mt16"><Note tone="gray" icon="info">已经越过插入位置的项目，新串入的节点显示为「可补做」，主线状态不往回退。停用后产物保留，节点标为「已停用」。</Note></div>
              ) : null}
            </div>
          ) : null}
          {tab === 'ct' ? <div className="card-b" style={{ padding: 18 }}><ContentsGrid groups={groups} /></div> : null}
          {tab === 'ver' ? <div className="card-b" style={{ padding: '8px 18px 14px' }}><Changelog changelog={d.changelog} /></div> : null}
        </div>

        <Modal
          open={menuOpen}
          onClose={() => setMenuOpen(false)}
          icon="puzzle"
          title={`本项目的${d.label}`}
          sub={heroMenuSub(d)}
          w={480}
          foot={<Button onClick={() => setMenuOpen(false)}>关闭</Button>}
        >
          <Menu
            style={{ boxShadow: 'none', border: 0, padding: 0 }}
            items={heroMenu(d, { pending }).map((x) => (x === '-' ? x : { ...x, onClick: () => pickMenu(x.key) }))}
          />
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
