// 我的项目（设计稿 projects）：P-01 列表与筛选，P-03 重命名实时检查，P-05/06/07/08/09 卡片菜单，用户流 EU-02/03
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellG } from '../../shell/Shell.jsx';
import {
  Button, Check, Empty, Field, Icon, Modal, Note, Prog, Seg, SegBar, Select, cx, useOutside,
} from '../../ui/index.jsx';
import { activityText, cellClass, projectLook } from './projectCard.js';

function ModeChip({ mode }) {
  return mode === 'L4'
    ? <span className="chip indigo sm" title="构建方式：自主构建（L4）"><Icon name="rocket" />自主</span>
    : <span className="chip blue sm" title="构建方式：陪伴建模（L2）"><Icon name="hand" />陪伴</span>;
}

function ProjectCardView({ p, onMenu, onRestore }) {
  const look = projectLook(p);
  const ab = p.autobuild;
  const live = ab && ['running', 'queued', 'pausing'].includes(ab.state);
  const done = ab && ab.state === 'completed' && ab.needs_review > 0;
  const cells = p.slices ? p.slices.cells.map(cellClass) : ['', '', '', ''];
  const open = () => navigate(pagePath(p.landing_page || 'wb-l2', p.id));
  const durText = (ms) => {
    const m = Math.round(ms / 60000);
    return m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m} 分`;
  };
  return (
    <div className={cx('card en-pc', p.archived && 'arch', live && 'l4-border')} onClick={p.archived ? undefined : open}>
      <div className="row g12">
        <span className="pi" style={{ background: look.color }}><Icon name={look.icon} size={18} /></span>
        <div className="grow" style={{ minWidth: 0 }}>
          <h3 className="ell">{p.name}</h3>
          <div className="xs muted">{p.industry_label || '未选行业'}</div>
        </div>
        <ModeChip mode={p.build_mode} />
        <button type="button" className="icon-btn en-more" title="更多操作" onClick={(e) => { e.stopPropagation(); onMenu(e.currentTarget, p); }}>
          <Icon name="more" />
        </button>
      </div>
      <div className="goal">{p.goal}</div>
      <div className="mt12">
        <SegBar states={cells} />
        <div className="row xs muted mt4">
          <span>切片进度</span><span className="sp" />
          <span>{p.slices ? `已确认 ${p.slices.confirmed}/${p.slices.total} 片` : '切片待规划'}</span>
        </div>
      </div>
      {live ? (
        <div className="en-live">
          <div className="row xs">
            <span className="row g4" style={{ color: 'var(--blue2)', fontWeight: 650 }}><i className="ld" />自主构建中</span>
            <span className="sp" />
            <span className="muted">第 {Math.min(ab.slice_total, ab.slice_done + 1)}/{ab.slice_total} 片{ab.eta_ms ? ` · 预计 ${Math.round(ab.eta_ms / 60000)} 分钟` : ''}</span>
          </div>
          <div className="mt8"><Prog p={ab.progress_pct} tone="l4" thin /></div>
          <div className="xs muted mt4">已自动决定 {ab.decisions} 项 · 挂起 {ab.suspended} 个问题 · 预算用了 {ab.budget_used_pct}%</div>
        </div>
      ) : null}
      {done ? (
        <div className="note amber mt8" style={{ padding: '8px 10px' }}>
          <Icon name="bot" />
          <div className="grow xs"><b>自主构建完成 · 需复核 {ab.needs_review} 项</b><br />{durText(ab.duration_ms)} · {ab.slice_done}/{ab.slice_total} 片完成</div>
        </div>
      ) : null}
      {p.publishable && !p.archived ? (
        <div className="note teal mt8" style={{ padding: '8px 10px' }}>
          <Icon name="check" />
          <div className="grow xs">
            <b>可发布</b> · 门禁全部通过{p.publishable.last_release ? `，上次发布 ${p.publishable.last_release.version}（${mmdd(p.publishable.last_release.at)}）` : ''}
          </div>
        </div>
      ) : null}
      <div className="ft">
        {p.inbox_count ? <span className="chip amber sm"><Icon name="inbox" />待处理 {p.inbox_count}</span> : null}
        {p.archived ? <Button size="xs" icon="refresh" onClick={(e) => { e.stopPropagation(); onRestore(p); }}>恢复</Button> : null}
        <span className="row g4"><Icon name="pkg" size={12} />知识包 {p.version_label}</span>
        <span className="sp" />
        <span>{activityText(p)}</span>
      </div>
    </div>
  );
}

function mmdd(iso) {
  const d = new Date(iso);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const FILTERS = [['all', '全部'], ['active', '进行中'], ['autobuild', '自主构建中'], ['publishable', '可发布'], ['archived', '已归档']];

export default function ProjectsPage() {
  const { api, services } = useApp();
  const [filter, setFilter] = useState('all');
  const [q, setQ] = useState('');
  const [sort, setSort] = useState('recent');
  const [data, setData] = useState(null);
  const [menu, setMenu] = useState(null);
  const [dlg, setDlg] = useState(null);
  const wrapRef = useRef(null);
  const menuRef = useRef(null);
  useOutside(menuRef, () => setMenu(null), !!menu);

  const load = useCallback(async () => {
    try {
      const r = await api.projects({ status: filter, q: q.trim() || undefined, sort });
      setData(r);
    } catch (e) {
      toast(errorText(e), 'alert');
      setData((d) => d || { items: [], counts: {} });
    }
  }, [api, filter, q, sort]);

  useEffect(() => {
    const t = setTimeout(load, q ? 200 : 0);
    return () => clearTimeout(t);
  }, [load, q]);

  const onUserEvent = useCallback((env) => {
    if (env.type === 'project.card.updated') {
      setData((d) => d && { ...d, items: d.items.map((x) => (x.id === env.data.project_id ? env.data.card : x)) });
    }
    if (env.type === 'project.removed') {
      setData((d) => d && { ...d, items: d.items.filter((x) => x.id !== env.data.project_id) });
    }
  }, []);

  const openMenu = (btn, p) => {
    const r = btn.getBoundingClientRect();
    const w = wrapRef.current.getBoundingClientRect();
    if (menu?.p.id === p.id) { setMenu(null); return; }
    setMenu({ p, top: r.bottom - w.top + 6, left: r.right - w.left - 236 });
  };

  const act = async (fn, okText, icon = 'check') => {
    try {
      await fn();
      if (okText) toast(okText, icon);
      load();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  const counts = data?.counts || {};
  const isEmpty = data && counts.all === 0 && counts.archived === 0 && !q;
  const items = data?.items || [];

  return (
    <ShellG active="projects" onUserEvent={onUserEvent}>
      <PageHead
        title="我的项目"
        desc="每个项目就是一个知识包，只有你能打开。"
        actions={<><Button icon="download" onClick={() => navigate('/projects/import')}>导入项目</Button><Button kind="primary" icon="plus" onClick={() => navigate('/projects/new')}>新建项目</Button></>}
      />
      {isEmpty ? (
        <div className="card" style={{ padding: 30 }}>
          <Empty
            icon="cube"
            title="还没有项目"
            actions={<>
              <Button kind="primary" icon="plus" onClick={() => navigate('/projects/new')}>新建项目</Button>
              <Button icon="download" onClick={() => navigate('/projects/import')}>导入项目</Button>
              <Button icon="play" kind="ghost" onClick={() => navigate('/projects/new', { start: 'example' })}>看示例项目</Button>
            </>}
          >
            创建项目并说明建模目标，AI 会围绕目标分析资料。一个项目就是一个知识包。
          </Empty>
        </div>
      ) : (
        <>
          <div className="row mb16 en-pjbar">
            <Seg items={FILTERS.map(([k, t]) => [k, `${t} ${counts[k] ?? ''}`.trim()])} value={filter} onChange={(k) => { setFilter(k); setMenu(null); }} />
            <div className="inp" style={{ width: 240 }}>
              <Icon name="search" size={14} />
              <input placeholder="搜索项目名称或目标" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <Select w="150px" icon="sortd" value={sort} onChange={setSort} options={[{ value: 'recent', label: '按最近活动' }, { value: 'name', label: '按名称' }, { value: 'created', label: '按创建时间' }]} />
            <span className="sp" />
          </div>
          <div className="en-pgw" ref={wrapRef}>
            <div className="en-pg">
              {items.map((p) => (
                <ProjectCardView key={p.id} p={p} onMenu={openMenu} onRestore={(x) => act(() => api.archiveProject(x.id, false), '已恢复到项目列表', 'refresh')} />
              ))}
              {filter !== 'archived' ? (
                <div className="en-ghost" onClick={() => navigate('/projects/new')}>
                  <Icon name="plus" size={24} />
                  <b>新建项目</b>
                  <span className="xs muted">说明建模目标，上传资料或连接业务系统</span>
                  <div className="row mt8">
                    <Button size="sm" kind="ghost" icon="download" onClick={(e) => { e.stopPropagation(); navigate('/projects/import'); }}>导入项目</Button>
                  </div>
                </div>
              ) : null}
            </div>
            {data && !items.length && filter !== 'all' ? <div className="muted sm" style={{ padding: '20px 4px' }}>没有符合条件的项目</div> : null}
            {menu ? (
              <div className="menu en-pmenu" ref={menuRef} style={{ top: menu.top, left: menu.left }} onClick={(e) => e.stopPropagation()}>
                <div className="mi" onClick={() => { setMenu(null); navigate(pagePath(menu.p.landing_page || 'wb-l2', menu.p.id)); }}><Icon name="arrow" size={14} /><span>打开</span></div>
                <div className="mi" onClick={() => { setMenu(null); setDlg({ kind: 'rename', p: menu.p }); }}><Icon name="edit" size={14} /><span>重命名</span></div>
                <div className="mi" onClick={() => { setMenu(null); setDlg({ kind: 'clone', p: menu.p }); }}><Icon name="copy" size={14} /><span>克隆</span></div>
                <hr />
                <div className="mi" onClick={() => { setMenu(null); act(() => services.download(`/projects/${encodeURIComponent(menu.p.id)}/transfer-package`, `${menu.p.name}-transfer.zip`), '已导出迁移包（含版本历史，不含凭据）', 'archive'); }}>
                  <Icon name="archive" size={14} /><span>导出迁移包（完整工作区）</span>
                </div>
                <hr />
                {menu.p.archived ? (
                  <div className="mi" onClick={() => { setMenu(null); act(() => api.archiveProject(menu.p.id, false), '已恢复到项目列表', 'refresh'); }}><Icon name="refresh" size={14} /><span>恢复</span></div>
                ) : (
                  <div className="mi" onClick={() => { setMenu(null); act(() => api.archiveProject(menu.p.id, true), '已归档，可在「已归档」里恢复', 'archive'); }}><Icon name="archive" size={14} /><span>归档</span></div>
                )}
                <div className="mi danger" onClick={() => { setMenu(null); setDlg({ kind: 'delete', p: menu.p }); }}><Icon name="trash" size={14} /><span>删除项目</span></div>
              </div>
            ) : null}
          </div>
        </>
      )}
      <DeleteDialog dlg={dlg?.kind === 'delete' ? dlg : null} onClose={() => setDlg(null)} onDone={load} />
      <RenameDialog dlg={dlg?.kind === 'rename' ? dlg : null} onClose={() => setDlg(null)} onDone={load} />
      <CloneDialog dlg={dlg?.kind === 'clone' ? dlg : null} onClose={() => setDlg(null)} onDone={load} />
    </ShellG>
  );
}

function DeleteDialog({ dlg, onClose, onDone }) {
  const { api } = useApp();
  const [v, setV] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setV(''); }, [dlg]);
  if (!dlg) return null;
  const name = dlg.p.name;
  const go = async () => {
    setBusy(true);
    try {
      await api.deleteProject(dlg.p.id, v);
      toast(`已删除「${name}」`, 'trash');
      onClose();
      onDone();
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open onClose={onClose} icon="trash" danger title={`删除「${name}」？`} sub="这个操作不可恢复" w={520}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="danger fill" icon="trash" disabled={v !== name || busy} onClick={go}>确认删除</Button></>}
    >
      <Note tone="red">无论项目当前状态如何都会直接删除：正在进行的建模会话、排队中的消息、<b>正在进行的自主构建</b>会立即强制终止。</Note>
      <div className="mt12 sm muted">删除后工作区文件、对话记录、版本历史和证据快照将被永久清除，回收站里也不会保留。数据源的凭据由网关保管，不受影响。</div>
      <div className="mt16">
        <Field label="输入项目名称确认删除">
          <div className="inp"><Icon name="edit" size={14} /><input placeholder={name} value={v} onChange={(e) => setV(e.target.value)} autoFocus /></div>
        </Field>
      </div>
    </Modal>
  );
}

function useNameCheck(name, excludeId, enabled) {
  const { api } = useApp();
  const [res, setRes] = useState(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    const t = setTimeout(() => {
      api.nameCheck(name, excludeId).then((r) => alive && setRes(r)).catch(() => {});
    }, 200);
    return () => { alive = false; clearTimeout(t); };
  }, [api, name, excludeId, enabled]);
  return res;
}

function nameHint(chk, name) {
  if (!chk) return null;
  if (chk.reason === 'empty') return <span style={{ color: 'var(--red)' }}>请输入项目名称</span>;
  if (chk.reason === 'too_long') return <span style={{ color: 'var(--red)' }}>项目名称不能超过 200 个字符</span>;
  if (chk.reason === 'conflict') return <span style={{ color: 'var(--red)' }}>你已经有一个叫「{name.trim()}」的项目，换个名字或加上日期</span>;
  return null;
}

function RenameDialog({ dlg, onClose, onDone }) {
  const { api } = useApp();
  const [v, setV] = useState('');
  useEffect(() => { if (dlg) setV(dlg.p.name); }, [dlg]);
  const chk = useNameCheck(v, dlg?.p.id, !!dlg && v !== dlg?.p.name);
  if (!dlg) return null;
  const same = v.trim() === dlg.p.name;
  const bad = !same && chk && !chk.available;
  const go = async () => {
    try {
      await api.patchProject(dlg.p.id, { name: v.trim() });
      toast('已改名', 'edit');
      onClose();
      onDone();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return (
    <Modal
      open onClose={onClose} icon="edit" title="重命名项目" sub="名称在你的项目里必须唯一" w={480}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={same || bad || !v.trim()} onClick={go}>改名并检查</Button></>}
    >
      <Field label="项目名称" hint={same ? null : nameHint(chk, v)}>
        <div className={cx('inp', bad && 'err')}><Icon name="edit" size={14} /><input value={v} onChange={(e) => setV(e.target.value)} autoFocus /></div>
      </Field>
      <div className="mt12 xs muted">改名不影响知识包包名 <span className="mono">{dlg.p.package_name}</span> 和版本历史。</div>
    </Modal>
  );
}

function CloneDialog({ dlg, onClose, onDone }) {
  const { api } = useApp();
  const [v, setV] = useState('');
  const [opt, setOpt] = useState({ include_history: true, include_snapshots: true, include_conversations: false });
  useEffect(() => { if (dlg) { setV(`${dlg.p.name}（副本）`); setOpt({ include_history: true, include_snapshots: true, include_conversations: false }); } }, [dlg]);
  const chk = useNameCheck(v, undefined, !!dlg);
  if (!dlg) return null;
  const bad = chk && !chk.available;
  const go = async () => {
    try {
      await api.cloneProject(dlg.p.id, { name: v.trim(), ...opt });
      toast('克隆完成');
      onClose();
      onDone();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return (
    <Modal
      open onClose={onClose} icon="copy" title={`克隆「${dlg.p.name}」`} sub="复制出一个新的知识包项目，只归你" w={500}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={bad || !v.trim()} onClick={go}>改名并克隆</Button></>}
    >
      <Field label="新项目名称" hint={nameHint(chk, v)}>
        <div className={cx('inp', bad && 'err')}><Icon name="edit" size={14} /><input value={v} onChange={(e) => setV(e.target.value)} /></div>
      </Field>
      <div className="col mt16" style={{ gap: 10 }}>
        <Check on={opt.include_history} onChange={(x) => setOpt((o) => ({ ...o, include_history: x }))} label="包含版本历史" />
        <Check on={opt.include_snapshots} onChange={(x) => setOpt((o) => ({ ...o, include_snapshots: x }))} label="包含证据快照" />
        <Check on={opt.include_conversations} onChange={(x) => setOpt((o) => ({ ...o, include_conversations: x }))} label="包含对话记录" />
      </div>
      <Note tone="gray" icon="info">数据源的凭据不会复制，新项目需要重新授权才能采集。启用的能力和原项目保持一致。</Note>
    </Modal>
  );
}
