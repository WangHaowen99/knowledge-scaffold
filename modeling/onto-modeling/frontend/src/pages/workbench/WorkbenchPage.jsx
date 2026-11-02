// 工作台 · 对话（设计稿 wb-l2）：一条顶栏、无气泡正文、产物卡、决策卡、悬浮输入框，全部由对话流事件驱动
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, relativeTime } from '../../api/errors.js';
import { commandNode, nodeActionKind, overlayRunning, sectionGroups, trackNodes } from '../../state/flow.js';
import { composerState, readonlyInfo } from '../../state/readonly.js';
import { ShellP } from '../../shell/Shell.jsx';
import {
  Button, Drawer, Field, Icon, Menu, Modal, Ring, Seg, SegBar, St, cx, useOutside,
} from '../../ui/index.jsx';
import { FLOWCHART_TYPE, latestFlowchartArtifact } from '../../app/packUi.js';
import { cellClass } from '../projects/projectCard.js';
import { AutobuildBar, AutobuildCtx, useAutobuild } from './autobuild.jsx';
import { ActionsCtx, ContextPanel, QueueCard } from './cards.jsx';
import { WbCtx } from './parts.jsx';
import { ScopePicker } from '../knowledge/ScopePicker.jsx';
import { Timeline } from './Timeline.jsx';
import { useConversation } from './useConversation.js';
import { MATERIAL_DIALOGS } from './materials/model.js';
import { useIncremental, useIncrementalTarget } from './incremental/useIncremental.jsx';
import '../../styles/workbench-extra.css';

// 双击插件产物（流程图卡、按声明画的产物卡）打开的编辑器或详情，按包的 views.detail 选（ADR-0055）
const ArtifactOpener = lazy(() => import('../../components/packs/ArtifactOpener.jsx'));
const MaterialsDialogs = lazy(() => import('./materials/MaterialsDialogs.jsx'));

const TRACK_CLASS = { confirmed: 'ok', running: 'running', pending: 'pending', blocked: 'blocked' };

function Track({ flow, onOpen }) {
  const nodes = trackNodes(flow);
  return (
    <div className="wx-track" onClick={onOpen} data-pop="点开看建模步骤和每个切片的进度">
      {nodes.map((n, i) => {
        const k = TRACK_CLASS[n.display_state] || '';
        const prevOk = i > 0 && TRACK_CLASS[nodes[i - 1].display_state] === 'ok';
        return (
          <span key={n.id} style={{ display: 'contents' }}>
            {i ? <span className={cx('wx-ln', prevOk && 'ok')} /> : null}
            <span className={cx('wx-st', k)}>
              <span className="dt">{k === 'ok' ? <Icon name="check" /> : k === 'blocked' ? <Icon name="lock" /> : null}</span>
              {n.title.replace('（按切片）', '')}
              {n.progress?.label ? <span className="x">{n.progress.label}</span> : null}
            </span>
          </span>
        );
      })}
    </div>
  );
}

// 建模步骤抽屉：切片 × 步骤（P2 起有）、按 sections 分组的节点；节点动作的执行方式见 nodeActionKind
function StepsDrawer({ open, onClose, flow, meta, cid }) {
  const { api } = useApp();
  const { pid, reloadFlow } = useProject();
  const [more, setMore] = useState(null);
  const moreRef = useRef(null);
  useOutside(moreRef, () => setMore(null), !!more);
  if (!flow) return <Drawer open={open} onClose={onClose} icon="branch" title="建模步骤" w={440}>正在读取…</Drawer>;
  const hasScenario = flow.slice_stages.some((s) => s.id === 'scenario');
  const recommended = (flow.notices || []).some((n) => n.kind === 'new_materials');
  const doAct = async (n, a) => {
    setMore(null);
    const kind = nodeActionKind(a);
    if (kind === 'navigate') { navigate(pagePath(a.target, pid)); return; }
    try {
      if (kind === 'command') {
        await api.sendCommand(pid, { command: a.target.trim().split(/\s+/)[0], text: a.target, origin: { page: 'wb-l2', label: '建模步骤' }, conversation_id: cid || undefined });
        onClose();
      } else if (kind === 'skip') await api.skipNode(pid, n.id, {});
      else if (kind === 'restore') await api.restoreNode(pid, n.id, {});
      else {
        await api.startNode(pid, n.id, { origin: { page: 'wb-l2', label: '建模步骤' } });
        onClose();
      }
      reloadFlow();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const row = (n) => {
    const label = n.progress?.label || (n.section === 'on_demand' && n.display_state === 'ready' && recommended && /incremental|update$/.test(n.id) ? '推荐' : undefined);
    // 面板入口的节点（如求解适配）不走对话命令；同一节点的多个命令只摆第一个，其余收进「⋯」
    const acts = n.panel ? [] : n.actions || [];
    const [first, ...rest] = acts;
    return (
      <div className="row" key={n.id} style={{ padding: '9px 2px', borderBottom: '1px dashed var(--line)' }}>
        <span className="grow"><b style={{ fontWeight: 600 }}>{n.title}</b>{n.note ? <div className="xs muted">{n.note}</div> : null}</span>
        {n.flags?.needs_review ? <St k="needs_review" sm /> : null}
        <St k={n.display_state} sm label={label} />
        {first ? <Button size="xs" kind={first.kind === 'skip' || first.kind === 'restore' ? 'ghost' : 'soft'} onClick={() => doAct(n, first)}>{first.label}</Button> : null}
        {rest.map((a) => (['skip', 'restore'].includes(a.kind) && !a.target ? <Button key={a.label} size="xs" kind="ghost" onClick={() => doAct(n, a)}>{a.label}</Button> : null))}
        {rest.some((a) => !['skip', 'restore'].includes(a.kind) || a.target) ? (
          <Button size="xs" kind="ghost" icon="more" title="更多操作" onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setMore({ node: n, left: r.right - 180, top: r.bottom + 4 }); }} />
        ) : null}
      </div>
    );
  };
  return (
    <Drawer open={open} onClose={onClose} icon="branch" title="建模步骤" sub={`${meta?.name || ''} · 步骤由已启用的能力决定`} w={440}>
      {flow.slices.length ? (
        <>
          <div className="sec-t"><Icon name="layers" size={14} />业务切片 {flow.slices.length}<span className="sp" /><Button size="xs" kind="ghost" icon="layers" onClick={() => { onClose(); navigate(pagePath('wb-blueprint', pid), { dialog: 'blueprint' }); }}>调整切片</Button></div>
          <div className="xs muted" style={{ margin: '-4px 0 4px' }}>每条小进度条依次是：{flow.slice_stages.map((s) => s.label).join('、')}</div>
          <div className="xs muted" style={{ margin: '0 0 6px', padding: '7px 9px', borderRadius: 8, background: 'var(--panel2)', lineHeight: 1.65 }}>
            <Icon name="info" size={12} /> 「规则」按资料出现，资料里没有规则就跳过，不挡这一片确认{hasScenario ? '；「场景」来自场景走查插件，未启用时不出现' : '；启用场景走查插件后，每片多出「场景」一步'}
          </div>
          {flow.slices.map((s, i) => (
            <div key={s.id} className={cx('wb-sr', s.group === 'now' && 'now')}>
              <span className="ix">{s.group === 'done' ? <Icon name="check" size={12} /> : i + 1}</span>
              <div className="grow" style={{ minWidth: 0 }}>
                <div className="b ell" style={{ fontSize: 13 }}>{s.name}</div>
                <div className="xs muted ell">{s.note || s.core_flow}</div>
              </div>
              <SegBar states={flow.slice_stages.map((st) => cellClass(s.stages[st.id]))} />
            </div>
          ))}
        </>
      ) : null}
      {sectionGroups(flow).map((g, gi) => (
        <div key={g.id}>
          <div className={cx('sec-t', (gi || flow.slices.length) && 'mt16')}><Icon name={SECTION_ICON[g.id] || 'flow'} size={14} />{g.title}</div>
          {g.nodes.map(row)}
        </div>
      ))}
      {more ? (
        <div ref={moreRef}>
          <Menu
            style={{ position: 'fixed', left: more.left, top: more.top, width: 180, zIndex: 80 }}
            items={(more.node.actions || []).slice(1).filter((a) => !['skip', 'restore'].includes(a.kind) || a.target).map((a) => ({ label: a.label, onClick: () => doAct(more.node, a) }))}
            onPick={() => setMore(null)}
          />
        </div>
      ) : null}
    </Drawer>
  );
}

// 时间线里最后一条用户消息的文字（命令按钮发出的消息带 command）
function lastUserText(state) {
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const it = state.items[state.order[i]];
    if (it?.kind === 'user') return it.command || it.text || '';
  }
  return null;
}

const SECTION_ICON = { required: 'flow', pre_publish: 'lock', optional: 'branch', on_demand: 'zap' };

// 会话切换菜单（chat-states 第 1 项）：新建、续聊、重命名、删除；运行中禁止
export function ConversationMenu({
  items, cid, running, ops, onClose, className = 'wb-pop wb-conv-menu', style, initialRenaming = null, initialDeleting = null, demoTitle, showOpsFor,
}) {
  const [renaming, setRenaming] = useState(initialRenaming);
  const [title, setTitle] = useState(demoTitle || '');
  const [deleting, setDeleting] = useState(initialDeleting);
  const act = async (fn) => {
    try { await fn(); } catch (e) { toast(errorText(e), 'alert'); }
  };
  const rename = (c) => act(async () => { await ops.rename(c.id, title.trim()); setRenaming(null); });
  return (
    <div className={className} style={style} onClick={(e) => e.stopPropagation()}>
    <div className="g2-cmd" style={{ padding: 6 }}>
      <div className="row" style={{ padding: '2px 4px 6px' }}>
        <b className="sm">本项目的会话 · {items.length}</b><span className="sp" />
        <Button size="xs" icon="plus" kind="soft" disabled={running} onClick={() => act(async () => { await ops.create(); onClose?.(); })}>新建会话</Button>
      </div>
      {items.map((c) => (
        <div key={c.id} className={cx('g2-conv', c.id === cid && 'on')} style={{ cursor: running || c.id === cid ? 'default' : 'pointer' }} onClick={() => { if (!running && c.id !== cid && renaming !== c.id) { ops.switch(c.id); onClose?.(); } }}>
          <Icon name={renaming === c.id ? 'edit' : 'msg'} size={14} />
          {renaming === c.id ? (
            <>
              <div className="grow"><div className="inp focus" style={{ height: 28, width: '100%' }}><input autoFocus={!demoTitle} value={title} maxLength={60} onChange={(e) => setTitle(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') rename(c); }} /></div></div>
              <Button size="xs" kind="soft" icon="check" disabled={!title.trim()} onClick={(e) => { e.stopPropagation(); rename(c); }} />
            </>
          ) : (
            <>
              <div className="grow">
                <div className={c.id === cid ? 'b' : undefined}>{c.title}</div>
                <div className="xs muted">{c.id === cid ? '当前会话 · ' : ''}{c.time || relativeTime(c.updated_at)}</div>
              </div>
              {c.id === cid && running ? <St k="running" sm label="运行中" /> : null}
              {c.id !== cid ? (
                <span className={cx('ops', showOpsFor === c.id && 'show')}>
                  <Button size="xs" kind="ghost" icon="edit" title="重命名" onClick={(e) => { e.stopPropagation(); setRenaming(c.id); setTitle(c.title); }} />
                  <Button size="xs" kind="ghost" icon="trash" title="删除" onClick={(e) => { e.stopPropagation(); setDeleting(c); }} />
                </span>
              ) : null}
            </>
          )}
        </div>
      ))}
    </div>
      {running ? <div className="mt8"><div className="note amber"><Icon name="alert" /><div className="grow">当前会话运行中，结束或打断后才能切换、新建或删除会话</div></div></div> : null}
      {deleting ? (
        <div className="g2-mm mt8" style={{ boxShadow: 'none', padding: '10px 12px' }}>
          <div className="b sm">删除这条会话？</div>
          <div className="xs muted mt4">对话记录与模型上下文一并删除，无法恢复。累计用量照常计入项目。</div>
          <div className="row mt8"><span className="sp" /><Button size="xs" onClick={() => setDeleting(null)}>取消</Button><Button size="xs" kind="danger" disabled={running} onClick={() => act(async () => { await ops.remove(deleting.id); setDeleting(null); })}>删除</Button></div>
        </div>
      ) : null}
    </div>
  );
}

function useConversationOps(conv) {
  const { api } = useApp();
  const { pid } = useProject();
  const { cid, setCid, reloadList } = conv;
  return {
    create: async () => { const c = await api.createConversation(pid); await reloadList(); setCid(c.id); },
    rename: async (id, title) => { await api.renameConversation(pid, id, title); await reloadList(); },
    remove: async (id) => { const r = await api.deleteConversation(pid, id); await reloadList(); if (id === cid) setCid(r.items[0]?.id); },
    switch: (id) => setCid(id),
  };
}

function elapsed(since, now) {
  if (!since) return '';
  const s = Math.max(0, Math.floor((now - Date.parse(since)) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function StateChip({ session, queue }) {
  const [now, setNow] = useState(Date.now());
  const running = session.state === 'running';
  useEffect(() => {
    if (!running) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  if (running) return <span className="chip blue sm wb-state-chip"><span className="spin" style={{ width: 9, height: 9 }} />{elapsed(session.turn_started_at, now) || '运行中'}</span>;
  if (session.state === 'queued') return <span className="chip amber sm wb-state-chip"><i className="dt" />排队中{queue?.position ? ` · 第 ${queue.position} 位` : ''}</span>;
  if (session.state === 'connecting') return <span className="chip gray sm wb-state-chip">连接中</span>;
  if (session.state === 'error') return <span className="chip red sm wb-state-chip"><Icon name="alert" />出错</span>;
  return null;
}

const KN_TYPES = [['correction', '纠错'], ['convention', '惯例'], ['term', '术语'], ['domain_rule', '领域规则']];
function KnowledgeDialog({ item, onClose }) {
  const { services } = useApp();
  const { pid } = useProject();
  const [title, setTitle] = useState('');
  const [type, setType] = useState('domain_rule');
  const [body, setBody] = useState('');
  const [scope, setScope] = useState({ kind: 'global' });
  const [total, setTotal] = useState(null);
  useEffect(() => {
    if (!item) return;
    const plain = item.text_md.replace(/\[\[|\]\]/g, '').replace(/\[\^\d+\]/g, '');
    setTitle(plain.split(/[。；\n]/)[0].slice(0, 60));
    setBody(`## 规则\n${plain}`);
    setScope({ kind: 'global' });
    services.client.get(`/projects/${encodeURIComponent(pid)}/knowledge/conflicts`).then((r) => setTotal(r.total_entries)).catch(() => setTotal(null));
  }, [item, pid, services]);
  if (!item) return null;
  const save = async () => {
    try {
      await services.client.post(`/projects/${encodeURIComponent(pid)}/knowledge`, {
        title: title.trim(), type, scope, rule_md: body, source: { conversation_id: item.conversation_id, message_id: item.message_id },
      });
      toast('已入库，后续建模会自动应用', 'book');
      onClose();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return (
    <Modal open onClose={onClose} icon="book" title="沉淀为业务知识" w={520} foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={!title.trim() || !body.trim()} onClick={save}>确认入库</Button></>}>
      <div className="col" style={{ gap: 10 }}>
        <Field label="条目名称"><div className="inp"><input value={title} maxLength={60} onChange={(e) => setTitle(e.target.value)} /></div></Field>
        <Field label="类型"><Seg items={KN_TYPES} value={type} onChange={setType} /></Field>
        <Field label="作用范围"><ScopePicker value={scope} onChange={setScope} /></Field>
        <Field label="内容"><div className="inp area" style={{ minHeight: 90 }}><textarea style={{ height: 72 }} value={body} onChange={(e) => setBody(e.target.value)} /></div></Field>
      </div>
      <div className="xs faint mt8">入库前先和已有的{total != null ? ` ${total} 条` : ''}知识查重、查冲突</div>
    </Modal>
  );
}

function Composer({ pid, cid, apiRef, st, buildMode, autobuild, modeLabel, onSend, catalog }) {
  const { api, services } = useApp();
  const [text, setText] = useState('');
  const [atts, setAtts] = useState([]);
  const [refs, setRefs] = useState([]);
  const [pop, setPop] = useState(null);
  const [refQ, setRefQ] = useState('');
  const [refHits, setRefHits] = useState([]);
  const fileRef = useRef(null);
  const taRef = useRef(null);
  const wrapRef = useRef(null);
  useOutside(wrapRef, () => setPop(null), !!pop);

  const addFiles = useCallback((files) => {
    [...(files || [])].slice(0, 10).forEach((file) => {
      const key = `${file.name}-${Math.random()}`;
      setAtts((a) => [...a, { key, name: file.name, image: /^image\//.test(file.type), progress: 0 }]);
      services.upload(file, 'attachment', { projectId: pid, onProgress: (p) => setAtts((a) => a.map((x) => (x.key === key ? { ...x, progress: p } : x))) })
        .then((up) => setAtts((a) => a.map((x) => (x.key === key ? { ...x, upload: up, progress: 100 } : x))))
        .catch((e) => { setAtts((a) => a.filter((x) => x.key !== key)); toast(errorText(e), 'alert'); });
    });
  }, [services, pid]);

  useEffect(() => {
    apiRef.current = {
      addFiles,
      clear: () => { setText(''); taRef.current?.focus(); },
      setText: (t) => { setText(t); if (t === '/') setPop('cmd'); taRef.current?.focus(); },
      addRef: (r) => { setRefs((x) => (x.some((y) => y.id === r.id) ? x : [...x, r])); taRef.current?.focus(); },
    };
  }, [addFiles, apiRef]);

  useEffect(() => {
    if (pop !== 'ref' || !refQ.trim()) { setRefHits([]); return undefined; }
    let alive = true;
    const t = setTimeout(() => api.search(pid, refQ.trim()).then((r) => {
      if (alive) setRefHits((r.groups || []).filter((g) => g.group === 'entries').flatMap((g) => g.items).slice(0, 8));
    }).catch(() => {}), 200);
    return () => { alive = false; clearTimeout(t); };
  }, [api, pid, pop, refQ]);

  const removeAtt = (a) => {
    if (a.upload) api.deleteUpload(a.upload.upload_id).catch(() => {});
    setAtts((x) => x.filter((y) => y.key !== a.key));
  };
  const uploading = atts.some((a) => !a.upload);
  const send = async () => {
    if (st.disabled || uploading) return;
    if (!text.trim() && !atts.length) return;
    const ok = await onSend({ text: text.trim(), attachments: atts.map((a) => a.upload.upload_id), references: refs });
    if (ok) { setText(''); setAtts([]); setRefs([]); }
  };
  const runCommand = async (c) => {
    setPop(null);
    try {
      await api.sendCommand(pid, { command: c.command, text: c.label, origin: { page: 'wb-l2', label: '全部命令' }, conversation_id: cid });
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  return (
    <div className="wb-comp" ref={wrapRef} style={{ position: 'relative' }}>
      {atts.length || refs.length ? (
        <div className="wb-atts">
          {refs.map((r) => <span key={r.id} className="g2-att"><Icon name="at" size={14} />{r.name}<Button size="xs" kind="ghost" icon="x" onClick={() => setRefs((x) => x.filter((y) => y.id !== r.id))} /></span>)}
          {atts.map((a) => (
            <span key={a.key} className="g2-att">
              {a.image ? <span className="g2-thumb" /> : <Icon name="filetext" size={14} />}
              {a.name}{!a.upload ? <span className="xs muted">{a.progress}%</span> : null}
              <Button size="xs" kind="ghost" icon="x" onClick={() => removeAtt(a)} />
            </span>
          ))}
        </div>
      ) : null}
      <textarea
        ref={taRef}
        placeholder={st.placeholder}
        disabled={st.disabled}
        value={text}
        onChange={(e) => { setText(e.target.value); if (e.target.value === '/') setPop('cmd'); }}
        onPaste={(e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
        }}
      />
      <input ref={fileRef} type="file" multiple hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} />
      <div className="wx-cb">
        <span className="wx-tb" title="附件" onClick={() => fileRef.current?.click()}><Icon name="clip" size={16} /></span>
        <span className="wx-tb" title="引用知识包里的条目" onClick={() => setPop(pop === 'ref' ? null : 'ref')}><Icon name="at" size={16} /></span>
        <span className="wx-tb" title="命令" onClick={() => setPop(pop === 'cmd' ? null : 'cmd')}><Icon name="cmd" size={16} /></span>
        <span className="sp" />
        <span className={cx('wx-mode', buildMode === 'L4' && 'l4')} onClick={() => navigate(pagePath('proj-settings', pid), { section: 'build-mode' })} data-pop="构建方式在「项目设置 › 构建方式」里改">
          <i />{modeLabel || (buildMode === 'L4' ? (autobuild ? '自主构建中' : '自主构建') : '陪伴建模')}
        </span>
        <span className="wx-send" title="发送" onClick={send} style={st.disabled || uploading ? { opacity: 0.5, cursor: 'not-allowed' } : undefined}><Icon name="arrow" size={16} /></span>
      </div>
      {pop === 'cmd' ? (
        <div className="g2-cmd wb-pop wb-cmd-pop">
          {(catalog?.groups || []).map((g) => (
            <div key={g.id}>
              <div className="gt">{g.label}</div>
              {g.items.map((c) => (
                <div key={c.id} className="it" style={{ cursor: c.available ? 'pointer' : 'not-allowed', opacity: c.available ? 1 : 0.5 }} title={c.unavailable_reason || ''} onClick={() => c.available && runCommand(c)}>
                  <b className="ell">{c.label}</b><span>{c.description}</span>
                </div>
              ))}
            </div>
          ))}
          <div className="row xs muted" style={{ padding: '6px 6px 0', borderTop: '1px solid var(--line)', marginTop: 4 }}>共 {catalog?.total ?? 0} 条<span className="sp" />点选即发到对话</div>
        </div>
      ) : null}
      {pop === 'ref' ? (
        <div className="menu wb-pop wb-ref-pop">
          <div className="inp" style={{ height: 32, margin: '2px 2px 6px' }}><Icon name="search" size={14} /><input autoFocus placeholder="搜索对象、操作、规则…" value={refQ} onChange={(e) => setRefQ(e.target.value)} /></div>
          {refHits.length ? refHits.map((h, i) => (
            <div key={i} className="mi" onClick={() => { if (h.target?.id) setRefs((x) => (x.some((y) => y.id === h.target.id) ? x : [...x, h.target])); setPop(null); setRefQ(''); }}>
              <Icon name="cube" size={14} /><span className="ell">{h.title}</span><span className="k">{h.snippet}</span>
            </div>
          )) : <div className="xs muted" style={{ padding: '4px 10px 6px' }}>{refQ ? '没有匹配的条目' : '输入名称搜索知识包里的条目'}</div>}
        </div>
      ) : null}
    </div>
  );
}

const INLINE_ALL = ['waiting_decision', 'pending_confirm'];
function SuggestionBar({ sug, onAction, onAll }) {
  if (!sug) return null;
  const items = [];
  if (sug.primary) items.push({ ...sug.primary, isPrimary: true });
  (sug.secondary || []).slice(0, 2).forEach((s) => items.push(s));
  const inline = INLINE_ALL.includes(sug.mode);
  return (
    <div className="wb-sug">
      {items.map((s) => {
        const dis = s.disabled;
        return (
          <span key={s.id} className={cx('s', s.isPrimary && !dis && 'p', dis && 'dis')} title={s.reason || undefined} onClick={() => !dis && onAction(s.action)}>
            {s.icon ? <Icon name={s.icon} size={12} /> : null}{s.label}
          </span>
        );
      })}
      {sug.all_commands !== false && inline ? <span className="s" onClick={onAll}><Icon name="cmd" size={12} />全部命令</span> : null}
      {sug.all_commands !== false && !inline ? <><span className="sp" /><span className="s" style={{ border: 0 }} onClick={onAll}><Icon name="cmd" size={12} />全部命令<Icon name="chevd" size={12} /></span></> : null}
    </div>
  );
}

export default function WorkbenchPage({ route }) {
  const { api, maintenance } = useApp();
  const project = useProject();
  const { pid, meta, flow, scope, inboxCount, hasPlugin } = project;
  // 增量更新（incremental）落到进行中的变更清单所在的对话
  const incTarget = useIncrementalTarget(route);
  const conv = useConversation(route.query?.c || incTarget.cid);
  const convOps = useConversationOps(conv);
  const { cid, state, list } = conv;
  const [drawer, setDrawer] = useState(false);
  const [menu, setMenu] = useState(null);
  const [restart, setRestart] = useState(false);
  const [knItem, setKnItem] = useState(null);
  const [catalog, setCatalog] = useState(null);
  const [usage, setUsage] = useState(null);
  const [drag, setDrag] = useState(false);
  const [suggestions, setSuggestions] = useState(null);
  const [opened, setOpened] = useState(null);
  const [mDialog, setMDialog] = useState(null);
  const ab = useAutobuild({ pid, cid, list });
  const mainRef = useRef(null);
  const inc = useIncremental({ route, target: incTarget, conv, state, flow, mainRef });
  const headRef = useRef(null);
  const comp = useRef({});
  const rounds = useRef(new Map());
  useOutside(headRef, () => setMenu(null), !!menu && menu !== 'ctx-hover');

  useEffect(() => { api.commandCatalog(pid).then(setCatalog).catch(() => {}); }, [api, pid]);
  useEffect(() => { if (cid) api.suggestions(pid, cid).then(setSuggestions).catch(() => {}); }, [api, pid, cid]);
  useEffect(() => { if (menu === 'ctx') api.projectUsage(pid).then(setUsage).catch(() => {}); }, [api, pid, menu]);
  // flowchart 页（设计稿）= 工作台 + 图示包的流程图编辑器；地址带 key 时打开那一张，
  // 不带时等对话历史到了，打开最近一张流程图卡的那张图；对话里没有就交给编辑器取 AR-02 的第一张；图示包没启用时说「插件已停用」
  const fcPicked = useRef(null);
  const closeOpened = useCallback(() => {
    setOpened(null);
    if (route.page === 'flowchart') navigate(pagePath('wb-l2', pid));
  }, [route.page, pid]);
  useEffect(() => {
    if (route.page !== 'flowchart') { fcPicked.current = null; return; }
    const want = route.query?.key || null;
    if (want) {
      if (fcPicked.current !== want) { fcPicked.current = want; setOpened({ type: FLOWCHART_TYPE, key: want }); }
      return;
    }
    if (fcPicked.current || !state.lastSeq) return;
    fcPicked.current = '*';
    setOpened(latestFlowchartArtifact(state) || { type: FLOWCHART_TYPE, key: null });
  }, [route.page, route.query?.key, state]);
  // 资料分析 / 建模蓝图的弹窗：地址带 ?dialog=material_map|verification|blueprint|build 时打开（建议条、建模步骤抽屉跳过来）
  useEffect(() => {
    const d = route.query?.dialog;
    if (MATERIAL_DIALOGS.includes(d)) setMDialog({ kind: d, next: route.query?.next });
  }, [route.page, route.query?.dialog, route.query?.next]);
  // Studio 的「问助手」：?ref=<条目 id> 或 ?ref=<产物类型>/<key>，取到名称后放进输入框的引用，再把参数去掉
  useEffect(() => {
    const ref = route.query?.ref;
    if (!ref) return undefined;
    let alive = true;
    const slash = ref.indexOf('/');
    const load = slash > 0 && !ref.includes(':')
      ? api.artifact(pid, ref.slice(0, slash), ref.slice(slash + 1)).then((a) => ({ kind: 'artifact', id: ref, key: ref.slice(slash + 1), name: a.title || a.key }))
      : api.studioEntry(pid, ref).then((e) => ({ kind: e.kind, id: e.id || ref, key: e.key, name: e.name }));
    load.then((r) => { if (alive) comp.current.addRef?.(r); }).catch(() => {}).finally(() => { if (alive) navigate(pagePath(route.page, pid)); });
    return () => { alive = false; };
  }, [api, pid, route.page, route.query?.ref]);
  const closeMDialog = useCallback(() => {
    setMDialog(null);
    if (route.query?.dialog) navigate(pagePath(route.page, pid));
  }, [route.page, route.query?.dialog, pid]);

  // 新内容到达时，停在底部附近就跟着滚到底
  const stick = useRef(true);
  useEffect(() => {
    const m = mainRef.current;
    if (m && stick.current) m.scrollTop = m.scrollHeight;
  }, [state.order.length, state.items]);
  const onScroll = () => {
    const m = mainRef.current;
    if (m) stick.current = m.scrollHeight - m.scrollTop - m.clientHeight < 120;
  };

  const running = ['running', 'connecting'].includes(state.session.state);
  const interrupt = useCallback(async () => {
    try { await api.interrupt(pid, cid); } catch (e) { toast(errorText(e), 'alert'); }
  }, [api, pid, cid]);

  useEffect(() => {
    // 自主构建运行中用运行条上的暂停、停在安全点控制，Esc 不打断
    const h = (e) => { if (e.key === 'Escape' && running && !ab.running && !document.querySelector('.overlay,.drawer')) interrupt(); };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [running, ab.running, interrupt]);

  const fail = (e) => toast(errorText(e), 'alert');
  const actions = useMemo(() => ({
    fail,
    answer: async (qid, body) => { try { await api.answerQuestion(pid, cid, qid, body); } catch (e) { fail(e); } },
    later: () => navigate(pagePath('inbox', pid)),
    merge: async (cs) => {
      try {
        const r = await api.mergeChangeset(pid, cs);
        toast(`已确认生效，保存为版本 ${r.version_label}`, 'merge');
      } catch (e) {
        fail(e);
        if (e.code === 'merge_conflict') navigate(pagePath('inbox', pid));
      }
    },
    discard: async (cs) => { try { await api.discardChangeset(pid, cs); toast('已撤回这一轮', 'undo'); } catch (e) { fail(e); } },
    permission: async (rid, behavior) => { try { await api.replyPermission(pid, cid, rid, { behavior }); } catch (e) { fail(e); } },
    resume: async (execId) => { try { await api.resumeExec(pid, execId, cid); } catch (e) { fail(e); } },
    abandon: async (execId) => { try { await api.abandonExec(pid, execId); toast('已放弃，下次从头开始', 'undo'); } catch (e) { fail(e); } },
    retry: async () => { try { await api.retryTurn(pid, cid); } catch (e) { fail(e); } },
    clearInput: () => comp.current.clear?.(),
    registerRound: (cs, fn) => { rounds.current.set(cs, fn); },
    canSaveKnowledge: hasPlugin('knowledge'),
    saveKnowledge: (item) => setKnItem({ ...item, conversation_id: cid }),
    openArtifact: (artifact) => setOpened(artifact),
    openMaterials: (d) => setMDialog(typeof d === 'string' ? { kind: d } : d),
    // 增量更新的卡片：清单弹窗、确认清单并执行、页面命令发到这条对话
    openChangeList: inc.openChangeList,
    executeChangeList: inc.execute,
    conversationId: cid,
  }), [api, pid, cid, hasPlugin]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSuggestion = async (a) => {
    try {
      switch (a.type) {
        case 'send':
          if (a.command) await api.sendCommand(pid, { command: a.command, args: a.args, text: a.text, origin: { page: 'wb-l2', label: '下一步建议' }, conversation_id: cid });
          else await api.sendMessage(pid, cid, { text: a.text, client_message_id: `cm-${Date.now()}` });
          break;
        case 'resume_exec': await actions.resume(a.exec_id); break;
        case 'merge_changeset': await actions.merge(a.changeset_id); break;
        case 'discard_changeset': await actions.discard(a.changeset_id); break;
        case 'show_changes': rounds.current.get(a.changeset_id)?.(); break;
        case 'start_node': await api.startNode(pid, a.node_id, { slice_id: a.slice_id, origin: { page: 'wb-l2', label: '下一步建议' } }); break;
        case 'navigate': navigate(pagePath(a.page, pid), a.params); break;
        case 'autobuild': await ab.act(a.op); break;
        case 'clear_input': comp.current.clear?.(); break;
        // 资料分析中「打断」（S-08）、建模蓝图「确认蓝图」（BP-03）
        case 'interrupt': {
          const r = await api.interrupt(pid, cid);
          if (!r?.interrupted) toast('现在没有正在跑的步骤', 'info');
          break;
        }
        case 'confirm_blueprint': setMDialog({ kind: 'build', next: a.next || 'L2' }); break;
        // 增量更新：确认清单并执行（INC-04）、开始增量更新
        case 'execute_change_list':
        case 'start_incremental': await inc.onAction(a); break;
        default: break;
      }
    } catch (e) {
      fail(e);
    }
  };

  const send = async (body) => {
    try {
      const r = await api.sendMessage(pid, cid, { ...body, client_message_id: `cm-${Date.now()}` });
      if (r.delivery === 'queued') toast('会话池已满，正在排队', 'clock');
      return true;
    } catch (e) {
      fail(e);
      return false;
    }
  };

  const sug = inc.decorate(state.suggestions || suggestions);
  // 对话正在跑的命令属于哪个节点（FL-01 commands）；服务端标出进行中之前，进度条先按它显示
  const runningCmd = ['running', 'connecting'].includes(state.session.state) ? lastUserText(state) : null;
  const viewFlow = useMemo(() => overlayRunning(flow, commandNode(flow, runningCmd)), [flow, runningCmd]);
  // 自主构建正在做的那一片由运行条和运行详情说明，顶栏不再放锁标记
  const lockInfo = scope && scope.state !== 'none' && !(ab.active && scope.state === 'autobuild_slice') ? readonlyInfo(scope) : null;
  // 运行中、排队时顶栏多了状态，「新资料」先收起，避免挤出顶栏；自主构建的状态在运行条上
  const busyHead = !ab.active && ['running', 'connecting', 'queued', 'error'].includes(state.session.state);
  const ctx = state.context;
  const ctxPct = Math.round(ctx?.pct ?? 0);
  const notice = (flow?.notices || []).find((n) => n.kind === 'new_materials');
  const convTitle = list?.items?.find((c) => c.id === cid)?.title || '对话';
  // 资料分析节点没确认：蓝图待确认时是切片规划，其余是读资料（含还没传资料）
  const research = (flow?.nodes || []).find((n) => n.id === 'core.research');
  const researching = !research || ['confirmed', 'skipped'].includes(research.state) ? false : research.state === 'pending_confirm' ? 'blueprint' : 'materials';
  const st = composerState({ sessionState: state.session.state, archived: meta?.archived, maintenance, autobuildRunning: !!state.session.autobuild || ab.running, researching });
  const wbCtx = useMemo(() => {
    const entries = {};
    state.order.forEach((k) => {
      const it = state.items[k];
      (it.entry_links || it.card?.entry_links || []).forEach((r) => { entries[r.name] = r; });
      if (it.card?.entry) entries[it.card.entry.name] = it.card.entry;
    });
    return { pid, entries };
  }, [pid, state.order, state.items]);

  const head = (
    <div className="wx-head" ref={headRef}>
      <div className="wx-sess" title="切换会话" onClick={() => setMenu(menu === 'conv' ? null : 'conv')}>
        <Icon name="msg" size={14} /><span className="nm ell">{convTitle}</span><Icon name="chevd" size={12} />
      </div>
      {viewFlow ? <Track flow={viewFlow} onOpen={() => setDrawer(true)} /> : <span />}
      <div className="wx-acts">
        {ab.active ? null : <StateChip session={state.session} queue={state.queue} />}
        {lockInfo ? (
          <span
            className="wx-ib nt"
            data-pop={`${lockInfo.label}：${lockInfo.text}。范围内 ${(scope.entries || []).length + (scope.artifacts || []).length} 个条目在 Studio 和插件页里只读，范围外照常编辑。`}
            onClick={() => { if (scope.state === 'pending_confirm' && scope.changeset_id) rounds.current.get(scope.changeset_id)?.(); }}
          >
            <Icon name="lock" size={14} />
          </span>
        ) : null}
        {notice && !busyHead ? (
          <span className="wx-ib nt" data-pop={notice.text} onClick={() => (notice.action?.type === 'navigate' ? navigate(pagePath(notice.action.page, pid)) : navigate(pagePath('incremental', pid)))}>
            <i />{notice.count} 份新资料
          </span>
        ) : null}
        <span className="wx-ib" onClick={() => navigate(pagePath('inbox', pid))}><Icon name="inbox" size={14} />待处理{inboxCount ? <span className="n">{inboxCount}</span> : null}</span>
        <span className="wx-sep" />
        <span className="wx-ib" title="建模步骤" onClick={() => setDrawer(true)}><Icon name="branch" size={16} /></span>
        <span className="wx-ib" data-pop={ctx ? `上下文 ${Math.round(ctx.used / 1000)}k / ${Math.round(ctx.window / 1000)}k tokens，接近 ${ctx.compact_at_pct}% 时自动压缩` : '上下文占用'} onClick={() => setMenu(menu === 'ctx' ? null : 'ctx')}>
          <Ring p={ctxPct} r={7} color={ctxPct >= 75 ? '#F59E0B' : '#2563EB'} /><span className="num">{ctxPct}%</span>
        </span>
        <span className="wx-ib" title="更多" onClick={() => setMenu(menu === 'more' ? null : 'more')}><Icon name="more" size={16} /></span>
      </div>
      {menu === 'conv' ? <ConversationMenu items={list?.items || []} cid={cid} running={['running', 'connecting', 'queued'].includes(state.session.state)} ops={convOps} onClose={() => setMenu(null)} /> : null}
      {menu === 'ctx' ? <div className="wb-pop wb-ctx-pop"><ContextPanel context={ctx} usage={usage} /></div> : null}
      {menu === 'more' ? (
        <div className="menu wb-pop wb-more-menu">
          <div className="mi" onClick={() => { setMenu(null); setRestart(true); }}><Icon name="refresh" size={14} /><span>重启会话</span><span className="k">按最新配置重连</span></div>
          <div className="mi" onClick={() => setMenu('ctx')}><Icon name="gauge" size={14} /><span>上下文与用量</span></div>
        </div>
      ) : null}
    </div>
  );

  return (
    <ShellP active="wb-l2" flush nosub>
      <ActionsCtx.Provider value={actions}>
        <AutobuildCtx.Provider value={ab}>
        <WbCtx.Provider value={wbCtx}>
          <div
            className="wbc"
            style={{ position: 'relative' }}
            onDragOver={(e) => { if (e.dataTransfer?.types?.includes('Files')) { e.preventDefault(); setDrag(true); } }}
            onDragLeave={(e) => { if (e.currentTarget === e.target) setDrag(false); }}
            onDrop={(e) => { e.preventDefault(); setDrag(false); comp.current.addFiles?.(e.dataTransfer.files); }}
          >
            {head}
            <AutobuildBar ab={ab} />
            <div className="wbc-main" ref={mainRef} onScroll={onScroll}>
              <div className="wbc-col">
                {conv.loadError ? <div className="note red mt16"><Icon name="alert" /><div className="grow">{errorText(conv.loadError)}</div></div> : null}
                {inc.hideTimeline ? null : <Timeline state={state} scope={scope} onInterrupt={interrupt} />}
                {state.session.state === 'queued' && state.queue ? <QueueCard queue={state.queue} onCancel={() => api.cancelQueue(pid, cid).catch(fail)} /> : null}
              </div>
            </div>
            <div className="wbc-foot">
              <div className="in">
                <SuggestionBar sug={sug} onAction={onSuggestion} onAll={() => comp.current.setText?.('/')} />
                <Composer pid={pid} cid={cid} apiRef={comp} st={st} buildMode={ab.active ? 'L4' : meta?.build_mode} autobuild={ab.running} modeLabel={ab.active ? (ab.ended ? '自主构建 · 已结束' : ab.paused ? '自主构建 · 已暂停' : '自主构建中') : null} onSend={send} catalog={catalog} />
              </div>
            </div>
            {drag ? <div className="wb-dropmask"><Icon name="upload" size={24} /><b>松开以添加附件</b><span className="xs muted">Markdown、Word、Excel、PDF、Drawio、TXT · 单个不超过 20 MB</span></div> : null}
          </div>
          <StepsDrawer open={drawer} onClose={() => setDrawer(false)} flow={viewFlow} meta={meta} cid={cid} />
          <Modal
            open={restart} onClose={() => setRestart(false)} icon="refresh" title="重启会话？" w={460}
            foot={<><Button onClick={() => setRestart(false)}>取消</Button><Button kind="primary" onClick={async () => { setRestart(false); try { await api.restartConversation(pid, cid); toast('会话已重置，下次发消息生效', 'check'); } catch (e) { fail(e); } }}>重启会话</Button></>}
          >
            <div className="sm muted">对话记录保留，按最新配置重连模型；稳定引擎会清空模型上下文。要从空白上下文开始，请新建会话。</div>
          </Modal>
          <KnowledgeDialog item={knItem} onClose={() => setKnItem(null)} />
          {opened ? (
            <Suspense fallback={null}>
              <ArtifactOpener artifact={opened} onClose={closeOpened} />
            </Suspense>
          ) : null}
          {mDialog ? <Suspense fallback={null}><MaterialsDialogs dialog={mDialog} onClose={closeMDialog} /></Suspense> : null}
          {inc.element}
        </WbCtx.Provider>
        </AutobuildCtx.Provider>
      </ActionsCtx.Provider>
    </ShellP>
  );
}
