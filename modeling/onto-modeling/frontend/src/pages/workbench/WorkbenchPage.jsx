// 工作台 · 对话（设计稿 wb-l2）：一条顶栏、无气泡正文、产物卡、决策卡、悬浮输入框，全部由对话流事件驱动
import {
  Suspense, lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, relativeTime } from '../../api/errors.js';
import { catalogCommand, commandNode, suggestionCommand, overlayRunning } from '../../state/flow.js';
import {
  filterCatalog, flatItems, menuKey, menuQuery,
} from '../../state/commandMenu.js';
import { composerState, lockPop, readonlyInfo } from '../../state/readonly.js';
import { rejectedResourceMessage, uploadRules, validateResourceFiles } from '../../lib/resourceUpload.js';
import { runStatus } from '../../state/conversation.js';
import { ShellP } from '../../shell/Shell.jsx';
import {
  Button, Icon, Modal, Ring, St, cx, useOutside,
} from '../../ui/index.jsx';
import { FLOWCHART_TYPE, latestFlowchartArtifact, pendingChangeset } from '../../app/packUi.js';
import { AutobuildBar, AutobuildCtx, useAutobuild } from './autobuild.jsx';
import { useModeSwitch } from './buildMode.jsx';
import { canInterrupt } from './autobuildModel.js';
import {
  barItems, needsRefetch, pickSuggestions, staleSuggestions, suggestionBarHidden,
} from './suggestionModel.js';
import { ActionsCtx, ContextPanel, QueueCard } from './cards.jsx';
import { WbCtx } from './parts.jsx';
import { EarlierHistory, Timeline } from './Timeline.jsx';
import { NextBar, RunLine } from './RunLine.jsx';
import { NavTrack } from './NavTrack.jsx';
import { SUBMITTING_TEXT, createSubmitGuard, likelyDuplicate, restoreText, sendButtonMode } from './composerModel.js';
import { useConversation } from './useConversation.js';
import { scrollFollow } from './scrollFollow.js';
import {
  BUSY_TEXT, isPluginConv, latestDraftCards, menuConversations, pickConversation, planEnterPlugin, planLeavePlugin, pluginReturn, returnTarget,
} from './pluginModeModel.js';
import {
  DraftFilesDrawer, PluginCtx, PluginModeNote, PluginStart, PluginTrack, usePluginDrafts,
} from './pluginMode.jsx';
import { MATERIAL_DIALOGS } from './materials/model.js';
import { useIncremental, useIncrementalTarget } from './incremental/useIncremental.jsx';
import '../../styles/workbench-extra.css';

// 双击插件产物（流程图卡、按声明画的产物卡）打开的编辑器或详情，按包的 views.detail 选（ADR-0055）
const ArtifactOpener = lazy(() => import('../../components/packs/ArtifactOpener.jsx'));
const MaterialsDialogs = lazy(() => import('./materials/MaterialsDialogs.jsx'));


// 时间线里最后一条用户消息的文字（命令按钮发出的消息带 command）
function lastUserText(state) {
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const it = state.items[state.order[i]];
    if (it?.kind === 'user') return it.command || it.text || '';
  }
  return null;
}

// 会话切换菜单（chat-states 第 1 项）：新建、续聊、重命名、删除；运行中禁止
export function ConversationMenu({
  items, cid, running, ops, onClose, className = 'wb-pop wb-conv-menu', style, initialRenaming = null, initialDeleting = null, demoTitle, showOpsFor, plugin,
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
        <b className="sm">{plugin ? '本项目的插件' : '本项目的会话'} · {items.length}</b><span className="sp" />
        <Button size="xs" icon="plus" kind="soft" disabled={running} onClick={() => act(async () => { await ops.create(); onClose?.(); })}>{plugin ? '新建插件' : '新建会话'}</Button>
      </div>
      {items.map((c) => (
        <div key={c.id} className={cx('g2-conv', c.id === cid && 'on')} style={{ cursor: running || c.id === cid ? 'default' : 'pointer' }} onClick={() => { if (!running && c.id !== cid && renaming !== c.id) { ops.switch(c.id); onClose?.(); } }}>
          <Icon name={renaming === c.id ? 'edit' : plugin ? 'puzzle' : 'msg'} size={14} />
          {renaming === c.id ? (
            <>
              <div className="grow"><div className="inp focus" style={{ height: 28, width: '100%' }}><input autoFocus={!demoTitle} value={title} maxLength={60} onChange={(e) => setTitle(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') rename(c); }} /></div></div>
              <Button size="xs" kind="soft" icon="check" disabled={!title.trim()} onClick={(e) => { e.stopPropagation(); rename(c); }} />
            </>
          ) : (
            <>
              <div className="grow">
                <div className={c.id === cid ? 'b' : undefined}>{c.title}</div>
                <div className="xs muted">{c.time || relativeTime(c.updated_at)}</div>
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
      {running ? <div className="mt8"><div className="note amber"><Icon name="alert" /><div className="grow">运行中，打断后才能切换会话</div></div></div> : null}
      {deleting ? (
        <div className="g2-mm mt8" style={{ boxShadow: 'none', padding: '10px 12px' }}>
          <div className="b sm">删除这条会话？</div>
          <div className="xs muted mt4">对话记录一并删除，无法恢复</div>
          <div className="row mt8"><span className="sp" /><Button size="xs" onClick={() => setDeleting(null)}>取消</Button><Button size="xs" kind="danger" disabled={running} onClick={() => act(async () => { await ops.remove(deleting.id); setDeleting(null); })}>删除</Button></div>
        </div>
      ) : null}
    </div>
  );
}

// plugin：在插件对话里，新建的是插件对话；删掉当前这条时回同类的下一条（ADR-0087）
function useConversationOps(conv, plugin, go) {
  const { api } = useApp();
  const { pid } = useProject();
  const { cid, reloadList } = conv;
  return {
    create: async () => { const c = await api.createConversation(pid, undefined, plugin ? 'plugin' : undefined); await reloadList(); go(c.id, c.kind); },
    rename: async (id, title) => { await api.renameConversation(pid, id, title); await reloadList(); },
    remove: async (id) => {
      const r = await api.deleteConversation(pid, id);
      await reloadList();
      if (id === cid) go((menuConversations(r.items, plugin)[0] || pickConversation(r.items))?.id);
    },
    switch: (id) => go(id),
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

const MODES = [['L2', 'users', '陪伴建模'], ['L4', 'rocket', '自主构建']];
// plugin：插件对话（ADR-0087）——模式胶囊显示「插件开发」，没有命令弹层。
// 全部命令（输入「/」）：每次打开都重取 S-18；斜杠后面接着打字按名字筛，↑↓ 选、回车执行、Esc 关；没有匹配时回车按原文发
function Composer({ pid, apiRef, st, buildMode, modeLabel, onMode, onSend, onCommand, onStop, plugin }) {
  const { api, services, config } = useApp();
  const [text, setText] = useState('');
  const [atts, setAtts] = useState([]);
  const [refs, setRefs] = useState([]);
  const [pop, setPop] = useState(null);
  const [refQ, setRefQ] = useState('');
  const [refHits, setRefHits] = useState([]);
  const [catalog, setCatalog] = useState(null);
  const [cmdSel, setCmdSel] = useState(0);
  const cmdRefs = useRef([]);
  const fileRef = useRef(null);
  const taRef = useRef(null);
  const wrapRef = useRef(null);
  useOutside(wrapRef, () => setPop(null), !!pop);

  const rules = useMemo(() => uploadRules(config), [config]);
  const addFiles = useCallback((files) => {
    // 附件落进资料区，与资料上传同一份规则：格式不对、太大的不传
    const { accepted, rejected } = validateResourceFiles([...(files || [])].slice(0, 10), rules);
    if (rejected.length) toast(rejectedResourceMessage(rejected), 'alert');
    accepted.forEach((file) => {
      const key = `${file.name}-${Math.random()}`;
      setAtts((a) => [...a, { key, name: file.name, image: /^image\//.test(file.type), progress: 0 }]);
      services.upload(file, 'attachment', { projectId: pid, onProgress: (p) => setAtts((a) => a.map((x) => (x.key === key ? { ...x, progress: p } : x))) })
        .then((up) => setAtts((a) => a.map((x) => (x.key === key ? { ...x, upload: up, progress: 100 } : x))))
        .catch((e) => { setAtts((a) => a.filter((x) => x.key !== key)); toast(errorText(e), 'alert'); });
    });
  }, [services, pid, rules]);

  useEffect(() => {
    apiRef.current = {
      addFiles,
      clear: () => { setText(''); taRef.current?.focus(); },
      setText: (t) => { setText(t); if (t === '/' && !plugin) setPop('cmd'); taRef.current?.focus(); },
      // 打断时撤回的话放回来，接在已经敲了的字后面
      restore: (texts) => { setText((t) => restoreText(t, texts)); taRef.current?.focus(); },
      addRef: (r) => { setRefs((x) => (x.some((y) => y.id === r.id) ? x : [...x, r])); taRef.current?.focus(); },
    };
  }, [addFiles, apiRef, plugin]);

  useEffect(() => {
    if (pop !== 'cmd') return undefined;
    let alive = true;
    api.commandCatalog(pid).then((c) => { if (alive) setCatalog(c); }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid, pop]);
  // 斜杠开头时按斜杠后面的字筛；从按钮打开、框里是别的字时列全部，回车照常发框里的字
  const query = menuQuery(text);
  const cmdGroups = useMemo(() => (pop === 'cmd' ? filterCatalog(catalog, query) : []), [pop, catalog, query]);
  const cmdItems = flatItems(cmdGroups);
  useEffect(() => { setCmdSel(0); }, [query, pop]);
  useEffect(() => { cmdRefs.current[cmdSel]?.scrollIntoView?.({ block: 'nearest' }); }, [cmdSel]);

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
  const [pressed, setPressed] = useState(0);
  // 点发送就清空输入框（气泡同时出现在时间线上）；没发出去再把原文放回来，期间新敲的字不覆盖
  const send = async () => {
    if (st.disabled || uploading) return;
    if (!text.trim() && !atts.length) return;
    const draft = { text, atts, refs };
    setPressed((n) => n + 1);
    setText(''); setAtts([]); setRefs([]);
    const ok = await onSend({ text: text.trim(), attachments: atts.map((a) => a.upload.upload_id), references: refs });
    if (!ok) {
      setText((t) => t || draft.text);
      setAtts((a) => (a.length ? a : draft.atts));
      setRefs((r) => (r.length ? r : draft.refs));
    }
  };
  const runCommand = (c) => {
    setPop(null);
    if (query !== null) setText('');
    onCommand({ ...catalogCommand(c), origin: { page: 'wb-l2', label: '全部命令' } });
  };
  const onText = (value) => {
    setText(value);
    if (plugin) return;
    if (value === '/') setPop('cmd');
    else if (pop === 'cmd' && query !== null && !value.startsWith('/')) setPop(null);
  };
  const cmdKey = (e) => {
    const a = menuKey(
      { key: e.key, shift: e.shiftKey, composing: e.nativeEvent.isComposing },
      { open: pop === 'cmd', filtering: query !== null, count: cmdItems.length, sel: cmdSel },
    );
    if (!a) return false;
    e.preventDefault();
    if (a.type === 'close') {
      // 只关菜单，不让工作台把 Esc 当成打断
      e.stopPropagation();
      setPop(null);
    } else if (a.type === 'move') setCmdSel(a.sel);
    else runCommand(cmdItems[a.index]);
    return true;
  };
  // 有字就发送（运行中：文字插进这一轮，命令排在这一轮之后）；没字时运行中是停止、空闲时灰掉
  const mode = sendButtonMode({ canStop: !!onStop, hasContent: !!text.trim() || atts.length > 0, disabled: st.disabled || uploading });
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
        onChange={(e) => onText(e.target.value)}
        onPaste={(e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); addFiles(fs); } }}
        onKeyDown={(e) => {
          if (cmdKey(e)) return;
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            if (pop === 'cmd') setPop(null);
            send();
          }
        }}
      />
      <input ref={fileRef} type="file" multiple hidden accept={rules.accept} onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }} />
      <div className="wx-cb">
        <span className="wx-tb" title="附件" onClick={() => fileRef.current?.click()}><Icon name="clip" size={16} /></span>
        <span className="wx-tb" title="引用知识包里的条目" onClick={() => setPop(pop === 'ref' ? null : 'ref')}><Icon name="at" size={16} /></span>
        {plugin ? null : <span className="wx-tb" title="全部命令" onClick={() => { if (pop === 'cmd') setPop(null); else { if (!text) setText('/'); setPop('cmd'); } }}><Icon name="cmd" size={16} /></span>}
        <span className="sp" />
        <span className={cx('wx-mode', plugin ? 'pm' : buildMode === 'L4' && 'l4')} onClick={() => !st.disabled && setPop(pop === 'mode' ? null : 'mode')}>
          <i />{modeLabel}
        </span>
        {mode === 'stop' ? (
          <span className="wx-send stop" title="停止（Esc）" onClick={onStop}><i /></span>
        ) : (
          <span key={pressed} className={cx('wx-send', pressed && 'mo-press', mode === 'off' && 'off')} title="发送" onClick={send}><Icon name="arrow" size={16} /></span>
        )}
      </div>
      {cmdItems.length ? (
        <div className="g2-cmd wb-pop wb-cmd-pop">
          {cmdGroups.map((g) => (
            <div key={g.id}>
              <div className="gt">{g.label}</div>
              {g.items.map((c) => {
                const i = cmdItems.indexOf(c);
                return (
                  <div key={c.id} ref={(el) => { cmdRefs.current[i] = el; }} className={cx('it', i === cmdSel && 'on')} style={{ cursor: 'pointer' }} onMouseEnter={() => setCmdSel(i)} onClick={() => runCommand(c)}>
                    <b className="ell">{c.label}</b>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      ) : null}
      {pop === 'mode' ? (
        <div className="menu wb-pop wb-mode-pop">
          {MODES.map(([k, icon, label]) => (
            <div key={k} className="mi" onClick={() => { setPop(null); onMode(k); }}>
              <Icon name={icon} size={14} /><span>{label}</span>{!plugin && buildMode === k ? <Icon name="check" size={14} className="k" /> : null}
            </div>
          ))}
          <hr />
          <div className="mi" onClick={() => { setPop(null); onMode('plugin'); }}>
            <Icon name="puzzle" size={14} /><span>插件开发</span>{plugin ? <Icon name="check" size={14} className="k" /> : null}
          </div>
        </div>
      ) : null}
      {pop === 'ref' ? (
        <div className="menu wb-pop wb-ref-pop">
          <div className="inp" style={{ height: 32, margin: '2px 2px 6px' }}><Icon name="search" size={14} /><input autoFocus placeholder="搜索对象、操作、规则…" value={refQ} onChange={(e) => setRefQ(e.target.value)} /></div>
          {refHits.length ? refHits.map((h, i) => (
            <div key={i} className="mi" onClick={() => { if (h.target?.id) setRefs((x) => (x.some((y) => y.id === h.target.id) ? x : [...x, h.target])); setPop(null); setRefQ(''); }}>
              <Icon name="cube" size={14} /><span className="ell">{h.title}</span><span className="k">{h.snippet}</span>
            </div>
          )) : refQ ? <div className="xs muted" style={{ padding: '4px 10px 6px' }}>没有匹配的条目</div> : null}
        </div>
      ) : null}
    </div>
  );
}

const INLINE_ALL = ['waiting_decision'];
// 包声明的快捷项（PL-12 entries，kind=quick_action）放在「全部命令」旁，点了发它的命令（R22）
// lead：排在最前的芯片（从插件开发切回来时的「回到插件开发」）
function SuggestionBar({ sug, quick = [], onAction, onAll, onQuick, lead }) {
  if (!sug && !quick.length && !lead) return null;
  const { items, note } = barItems(sug);
  const inline = !sug || INLINE_ALL.includes(sug.mode);
  const quickEls = quick.map((q) => <span key={q.id} className="s" onClick={() => onQuick(q)}><Icon name="zap" size={12} />{q.label}</span>);
  return (
    <div className="wb-sug">
      {lead}
      {items.map((s) => {
        const dis = s.disabled;
        return (
          <span key={s.id} className={cx('s', s.isPrimary && !dis && 'p', dis && 'dis')} title={s.reason || undefined} onClick={() => !dis && onAction(s.action, s.label)}>
            {s.icon ? <Icon name={s.icon} size={12} /> : null}{s.label}
          </span>
        );
      })}
      {inline ? quickEls : null}
      {sug?.all_commands !== false && inline ? <span className="s" onClick={onAll}><Icon name="cmd" size={12} />全部命令</span> : null}
      {!inline ? <span className="sp" /> : null}
      {!inline ? quickEls : null}
      {sug?.all_commands !== false && !inline ? <span className="s" style={{ border: 0 }} onClick={onAll}><Icon name="cmd" size={12} />全部命令<Icon name="chevd" size={12} /></span> : null}
      {note ? <div className="wb-sug-note">{note}</div> : null}
    </div>
  );
}

export default function WorkbenchPage({ route }) {
  const { api, maintenance } = useApp();
  const project = useProject();
  const { pid, meta, flow, scope, inboxCount, extensions } = project;
  // 增量更新（incremental）落到进行中的变更清单所在的对话
  const incTarget = useIncrementalTarget(route);
  const conv = useConversation(route.query?.c || incTarget.cid, { preferAutobuild: route.page === 'l4-run' });
  const { cid, state, list } = conv;
  // 插件开发（ADR-0087）：插件对话是另一类对话，切模式就是换对话；进插件对话、或地址里已经指定了对话时，换对话也写进地址（?c=）
  const convItem = list?.items?.find((c) => c.id === cid) || null;
  const plugin = isPluginConv(convItem);
  const goConversation = useCallback((id, kind) => {
    if (!id) return;
    conv.setCid(id);
    const toPlugin = kind ? kind === 'plugin' : isPluginConv(list?.items?.find((c) => c.id === id));
    if (toPlugin || route.query?.c) navigate(pagePath(toPlugin ? 'wb-l2' : route.page, pid), { c: id });
  }, [conv.setCid, list, route.page, route.query?.c, pid]); // eslint-disable-line react-hooks/exhaustive-deps
  const convOps = useConversationOps(conv, plugin, goConversation);
  const drafts = usePluginDrafts();
  const draftName = plugin ? convItem.plugin : null;
  useEffect(() => { if (draftName) drafts.ensure(draftName); }, [draftName, drafts.ensure]); // eslint-disable-line react-hooks/exhaustive-deps
  const boundDraft = draftName && drafts.drafts[draftName]?.name ? drafts.drafts[draftName] : null;
  const [filesDrawer, setFilesDrawer] = useState(null);
  // 进了插件开发，「回到插件开发」的记录就用过了
  useEffect(() => { if (plugin) pluginReturn.clear(window.sessionStorage, pid); }, [plugin, pid]);
  const [menu, setMenu] = useState(null);
  const [restart, setRestart] = useState(false);
  const [usage, setUsage] = useState(null);
  const [drag, setDrag] = useState(false);
  const [suggestions, setSuggestions] = useState(null);
  const [opened, setOpened] = useState(null);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const ab = useAutobuild({ pid, cid, list });
  const [mDialog, setMDialog] = useState(null);
  const modeSw = useModeSwitch({ ab, openDialog: (d) => setMDialog(typeof d === 'string' ? { kind: d } : d) });
  // 模式菜单（ADR-0087）：选「插件开发」进最近的插件对话（没有就新建）；在插件对话里选陪伴建模、自主构建，先回最近的建模对话再按原来的逻辑切
  const enterPlugin = async (id) => {
    const plan = planEnterPlugin({ items: list?.items, sessionState: state.session.state, autobuildRunning: !!state.session.autobuild || ab.running });
    if (plan.action === 'busy') { toast(BUSY_TEXT[plan.reason], 'clock'); return; }
    try {
      if (id || plan.action === 'open') goConversation(id || plan.id, 'plugin');
      else { const c = await api.createConversation(pid, undefined, 'plugin'); await conv.reloadList(); goConversation(c.id, 'plugin'); }
    } catch (e) { toast(errorText(e), 'alert'); }
  };
  const leavePlugin = async (target) => {
    const plan = planLeavePlugin({ items: list?.items, sessionState: state.session.state });
    if (plan.action === 'busy') { toast(BUSY_TEXT[plan.reason], 'clock'); return false; }
    try {
      if (plan.action === 'open') goConversation(plan.id, 'chat');
      else { const c = await api.createConversation(pid); await conv.reloadList(); goConversation(c.id, 'chat'); }
    } catch (e) { toast(errorText(e), 'alert'); return false; }
    if (target) modeSw.switchTo(target);
    return true;
  };
  const onMode = (k) => {
    if (k === 'plugin') { if (!plugin) enterPlugin(); return; }
    if (plugin) leavePlugin(k); else modeSw.switchTo(k);
  };
  // 草稿卡上的「切回建模试用」：记下从哪条插件对话来，建模对话的建议条多一个「回到插件开发」
  const backRef = useRef(null);
  backRef.current = async () => {
    const from = cid;
    if (await leavePlugin(null)) pluginReturn.set(window.sessionStorage, pid, from);
  };
  const returnId = plugin ? null : returnTarget(list?.items, pluginReturn.get(window.sessionStorage, pid));
  const mainRef = useRef(null);
  const inc = useIncremental({ route, target: incTarget, conv, state, mainRef });
  const headRef = useRef(null);
  const comp = useRef({});
  const rounds = useRef(new Map());
  useOutside(headRef, () => setMenu(null), !!menu && menu !== 'ctx-hover');

  // FL-02：进对话时取一份；项目流说建议变了（别处改了主线、装配变了）时再取，推来的和取到的按版本号取新的
  const refetchSuggestions = useCallback(() => {
    if (cid) api.suggestions(pid, cid).then(setSuggestions).catch(() => {});
  }, [api, pid, cid]);
  useEffect(() => { setSuggestions(null); refetchSuggestions(); }, [refetchSuggestions]);
  useEffect(() => project.subscribe((env) => {
    if (env.type === 'flow.updated' && env.data?.suggestions_changed) refetchSuggestions();
  }), [project.subscribe, refetchSuggestions]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (menu === 'ctx') api.projectUsage(pid).then(setUsage).catch(() => {}); }, [api, pid, menu]);
  // flowchart 页（设计稿）= 工作台 + 图示包的流程图编辑器；地址带 key 时打开那一张，
  // 不带时等对话历史到了，打开最近一张流程图卡的那张图；对话里没有就交给编辑器取 AR-02 的第一张；图示包没启用时说「插件已停用」
  const fcPicked = useRef(null);
  const closeOpened = useCallback(() => {
    setOpened(null);
    if (route.page === 'flowchart') navigate(pagePath('wb-l2', pid));
  }, [route.page, pid]);
  useEffect(() => {
    // 离开 flowchart 页（地址换成别的工作台页）时，由地址打开的编辑器跟着关
    if (route.page !== 'flowchart') { if (fcPicked.current) setOpened(null); fcPicked.current = null; return; }
    const want = route.query?.key || null;
    if (want) {
      if (fcPicked.current !== want) { fcPicked.current = want; setOpened({ type: FLOWCHART_TYPE, key: want }); }
      return;
    }
    if (fcPicked.current || !state.lastSeq) return;
    fcPicked.current = '*';
    setOpened(latestFlowchartArtifact(state) || { type: FLOWCHART_TYPE, key: null });
  }, [route.page, route.query?.key, state]);
  // 资料分析 / 建模蓝图的弹窗：地址带 ?dialog=material_map|verification|blueprint|build 时打开（建议条跳过来）
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
  // 推到下一帧再滚：同一帧里的多次更新只滚一次，读 scrollHeight 引起的整页排版也不和提交挤在同一个长任务里
  const stickFrame = useRef(0);
  useEffect(() => {
    if (!stick.current || stickFrame.current) return undefined;
    stickFrame.current = requestAnimationFrame(() => {
      stickFrame.current = 0;
      const m = mainRef.current;
      // 跟到底不走平滑滚动：流式输出时每帧都在追，平滑动画会一直落在后面
      if (m && stick.current) m.scrollTo({ top: m.scrollHeight, behavior: 'instant' });
    });
    return undefined;
    // 草稿卡的实时状态（PD-02）比历史晚到、卡片会变高：到了也跟一次
  }, [state.order.length, state.items, state.thinking, drafts.drafts]);
  // 清理时把帧号归零：开发模式（StrictMode）会先卸载再挂载一次，留着旧帧号会让上面一直提前返回、再也不跟到底
  useEffect(() => () => { cancelAnimationFrame(stickFrame.current); stickFrame.current = 0; }, []);
  // 离开底部时出「回到底部」；点下去后平滑滚动的途中仍跟着新内容，往上翻就不再跟
  const [away, setAway] = useState(false);
  const follow = useRef({ jumping: false, top: 0 });
  const onScroll = () => {
    const m = mainRef.current;
    if (!m) return;
    const f = scrollFollow(follow.current, { top: m.scrollTop, gap: m.scrollHeight - m.scrollTop - m.clientHeight });
    follow.current = f;
    stick.current = f.stick;
    setAway(!f.stick);
  };
  const toBottom = () => {
    const m = mainRef.current;
    if (!m) return;
    follow.current = { jumping: true, top: m.scrollTop };
    stick.current = true;
    setAway(false);
    m.scrollTo({ top: m.scrollHeight });
  };
  // 往前翻页（S-06）后保持视线不动：记下最上面那一块的位置，旧记录插到前面后把它挪回原处
  const earlierAnchor = useRef(null);
  const { loadEarlier } = conv;
  const onLoadEarlier = useCallback(() => {
    const el = mainRef.current?.querySelector('.wb-earlier + *');
    earlierAnchor.current = el ? { el, top: el.getBoundingClientRect().top } : null;
    loadEarlier();
  }, [loadEarlier]);
  useLayoutEffect(() => {
    const a = earlierAnchor.current;
    const m = mainRef.current;
    earlierAnchor.current = null;
    if (a && m && a.el.isConnected) m.scrollTop += a.el.getBoundingClientRect().top - a.top;
  }, [state.historyGen]);

  const running = ['running', 'connecting'].includes(state.session.state);
  // 停止（S-08）：还没被助手看到的插话撤回，放回输入框
  // 发消息、发命令、建议条共用一个提交中拦截（develop !596）
  const submitGuard = useMemo(createSubmitGuard, []);
  const submitOnce = useCallback(async (fn, skipped) => {
    if (submitGuard.busy()) {
      toast(SUBMITTING_TEXT, 'info');
      return skipped;
    }
    return submitGuard.run(fn);
  }, [submitGuard]);
  const lastSent = useRef(null);
  const interrupt = useCallback(async () => {
    try {
      const r = await api.interrupt(pid, cid);
      const back = (r?.returned || []).map((x) => x.text).filter(Boolean);
      if (back.length) comp.current.restore?.(back);
    } catch (e) { toast(errorText(e), 'alert'); }
  }, [api, pid, cid]);
  // 插话等太久时的「打断并发送」：停下这一轮，这句话作为新的一轮发出；其余撤回的话放回输入框
  const stopAndSend = useCallback(async (messageId) => {
    try {
      const r = await api.interrupt(pid, cid);
      const returned = r?.returned || [];
      const mine = returned.find((x) => x.message_id === messageId);
      const rest = returned.filter((x) => x !== mine).map((x) => x.text).filter(Boolean);
      if (rest.length) comp.current.restore?.(rest);
      if (mine?.text) await api.sendMessage(pid, cid, { text: mine.text, client_message_id: `cm-${Date.now()}` });
    } catch (e) { toast(errorText(e), 'alert'); }
  }, [api, pid, cid]);
  // 发命令（全部命令、快捷项）：运行中由平台代存到这一轮结束；已经排着一条时问要不要替换
  const [replaceAsk, setReplaceAsk] = useState(null);
  const sendCommand = useCallback((body, replace = false) => submitOnce(async () => {
    try {
      await api.sendCommand(pid, { ...body, conversation_id: cid, ...(replace ? { replace: true } : {}) });
    } catch (e) {
      if (e?.code === 'next_occupied' && !replace) setReplaceAsk({ body, label: e.details?.label || '' });
      else toast(errorText(e), 'alert');
    }
  }), [api, pid, cid, submitOnce]);
  const nextOp = useCallback(async (op) => {
    try { await (op === 'start' ? api.startNext(pid, cid) : api.cancelNext(pid, cid)); } catch (e) { toast(errorText(e), 'alert'); }
  }, [api, pid, cid]);

  useEffect(() => {
    // 自主构建运行中用运行条上的暂停、停在安全点控制，Esc 不打断
    const h = (e) => { if (e.key === 'Escape' && running && !ab.running && !document.querySelector('.overlay,.drawer')) interrupt(); };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [running, ab.running, interrupt]);

  const fail = (e) => toast(errorText(e), 'alert');
  // 决策卡草稿（question_id → 选了什么、写了什么）：问题失效后卡片只读留着这些内容
  const answerDrafts = useRef(new Map());
  const actions = useMemo(() => ({
    fail,
    answerDrafts: answerDrafts.current,
    // 出错交给卡片处理：问题已失效（410）时卡片留着草稿只读显示，其他错误卡片提示后可再提交
    answer: (qid, body) => api.answerQuestion(pid, cid, qid, body),
    later: () => navigate(pagePath('inbox', pid)),
    // 撤回这一轮（CS-05）：回到上一个版本，另存一个版本；之后又改过同一条时服务端说明原因、不撤
    discard: async (cs) => {
      try {
        const r = await api.discardChangeset(pid, cs);
        toast(r?.version_label ? `已撤回这一轮 · 版本 ${r.version_label}` : '已撤回这一轮', 'undo');
      } catch (e) { fail(e); }
    },
    resolve: () => navigate(pagePath('inbox', pid)),
    permission: async (rid, behavior) => { try { await api.replyPermission(pid, cid, rid, { behavior }); } catch (e) { fail(e); } },
    resume: async (execId) => { try { await api.resumeExec(pid, execId, cid); } catch (e) { fail(e); } },
    abandon: async (execId) => { try { await api.abandonExec(pid, execId); toast('已放弃', 'undo'); } catch (e) { fail(e); } },
    retry: async () => { try { await api.retryTurn(pid, cid); } catch (e) { fail(e); } },
    clearInput: () => comp.current.clear?.(),
    registerRound: (cs, fn) => { rounds.current.set(cs, fn); },
    // 卡上的变更集还待确认时，带上它读这一轮的版本（AR-03 ?changeset=）
    openArtifact: (artifact, changesetId) => {
      const cs = pendingChangeset(scopeRef.current, changesetId);
      setOpened(cs ? { ...artifact, changeset: cs } : artifact);
    },
    openMaterials: (d) => setMDialog(typeof d === 'string' ? { kind: d } : d),
    // 增量更新的卡片：清单弹窗、确认清单并执行、页面命令发到这条对话
    openChangeList: inc.openChangeList,
    executeChangeList: inc.execute,
    conversationId: cid,
    stopAndSend,
  }), [api, pid, cid, plugin, stopAndSend]); // eslint-disable-line react-hooks/exhaustive-deps

  // 插件对话的草稿卡、文件抽屉共用：草稿状态缓存、每份草稿最新的一张卡、发话、切回建模
  const sendRef = useRef(null);
  const latestCards = useMemo(() => latestDraftCards(state), [state.order, state.items]); // eslint-disable-line react-hooks/exhaustive-deps
  const pluginCtx = useMemo(() => ({
    drafts: drafts.drafts,
    ensure: drafts.ensure,
    reload: drafts.reload,
    put: drafts.put,
    latestCards,
    openFiles: (name, changes) => setFilesDrawer({ name, changes: !!changes, open: true }),
    sendText: (text) => sendRef.current?.({ text }),
    backToModel: () => backRef.current?.(),
    discard: (cs) => actions.discard(cs),
  }), [drafts.drafts, drafts.ensure, drafts.reload, drafts.put, latestCards, actions]);

  // 等你回答：滚到那张卡并闪一下；找不到这一问时去最后一张没答的
  const focusQuestion = (id) => {
    const root = mainRef.current;
    const all = root ? [...root.querySelectorAll('[data-qid]')] : [];
    const el = all.find((x) => x.dataset.qid === id) || all[all.length - 1];
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.remove('wb-flash');
    void el.offsetWidth; // eslint-disable-line no-void -- 重新触发动画
    el.classList.add('wb-flash');
  };

  // 建议条发起的请求都带 from_suggestion：点下去之前情况变了，接口回 suggestion_stale 并带回新的一份，直接换上
  const onSuggestion = (a, label) => submitOnce(async () => {
    const flag = { from_suggestion: true };
    try {
      switch (a.type) {
        case 'send':
          if (a.command) await api.sendCommand(pid, { ...suggestionCommand(a), display_label: label, origin: { page: 'wb-l2', label: '下一步建议' }, conversation_id: cid, ...flag });
          else await api.sendMessage(pid, cid, { text: a.text, client_message_id: `cm-${Date.now()}`, ...flag });
          break;
        case 'resume_exec': await api.resumeExec(pid, a.exec_id, cid, flag); break;
        case 'abandon_exec': await api.abandonExec(pid, a.exec_id, { ...flag, conversation_id: cid }); toast('已放弃，已经保存的修改保留', 'undo'); break;
        case 'retry': await api.retryTurn(pid, cid, flag); break;
        case 'focus_question': focusQuestion(a.question_id); break;
        case 'discard_changeset': await actions.discard(a.changeset_id); break;
        case 'show_changes': rounds.current.get(a.changeset_id)?.(); break;
        case 'start_node': await api.startNode(pid, a.node_id, { slice_id: a.slice_id, origin: { page: 'wb-l2', label: '下一步建议' } }); break;
        case 'navigate': navigate(pagePath(a.page, pid), a.params); break;
        case 'autobuild': await ab.act(a.op); break;
        case 'clear_input': comp.current.clear?.(); break;
        // 资料分析中「打断」（S-08）
        case 'interrupt': {
          const r = await api.interrupt(pid, cid);
          if (!r?.interrupted) toast('现在没有正在跑的步骤', 'info');
          break;
        }
        // F8 之前缓存下来的「确认蓝图」：蓝图不用确认了，自主构建直接走开始前预览，其余打开蓝图看一眼
        case 'confirm_blueprint': setMDialog(a.next === 'L4' ? { kind: 'build', next: 'L4' } : { kind: 'blueprint' }); break;
        // 增量更新：确认清单并执行（INC-04）
        case 'execute_change_list': await inc.onAction(a); break;
        default: break;
      }
    } catch (e) {
      const fresh = staleSuggestions(e);
      if (!fresh) { fail(e); return; }
      setSuggestions(fresh);
      toast('情况变了，建议已更新', 'info');
    }
  });

  // 导航栏下拉（NavTrack）：按钮发到对话（斜杠命令走 S-20、核验那句当普通消息发）或跳页；可选节点点名字开始；
  // 跳过、恢复、「这一轮不处理」调 FL-03 / FL-04，之后重取流程（项目流只推状态，不推动作）
  const navOrigin = { page: 'wb-l2', label: '导航栏' };
  const runNavButton = (b) => {
    if (b.kind === 'page') navigate(pagePath(b.target, pid));
    else if (b.kind === 'message') send({ text: b.target });
    else sendCommand({ ...catalogCommand(b), origin: navOrigin });
  };
  const onNav = async (a) => {
    try {
      if (a.type === 'button') runNavButton(a.button);
      else if (a.type === 'start') {
        const go = a.row.start;
        if (go.kind === 'page') navigate(pagePath(go.page, pid), go.params);
        else if (go.kind === 'command') runNavButton({ ...go, kind: 'command' });
        else await submitOnce(() => api.startNode(pid, go.id, { conversation_id: cid, origin: navOrigin }));
      } else {
        await (a.type === 'restore' ? api.restoreNode(pid, a.id) : api.skipNode(pid, a.id));
        project.reloadFlow?.();
      }
    } catch (e) { fail(e); }
  };

  const quickActions = useMemo(() => (extensions?.entries || []).filter((e) => e.kind === 'quick_action' && e.command), [extensions]);
  const runQuick = (q) => sendCommand({ command: q.command, display_label: q.label, origin: { page: 'wb-l2', label: q.label, pack: q.pack } });

  const send = (body) => submitOnce(async () => {
    const localId = `cm-${Date.now()}`;
    // 纯文字消息先在本地显示气泡（带发送动画），服务端回传同一条后原地换掉；带附件的要等服务端存好附件名。
    // 这一轮没停下又发了和上一条相同的文字：服务端会按重复拒收，先不画
    const repeat = likelyDuplicate(lastSent.current, { cid, text: body.text, sessionState: state.session.state });
    const local = !!body.text && !body.attachments?.length && !repeat;
    if (local) conv.dispatch({ type: 'local_user', id: localId, text: body.text });
    try {
      const r = await api.sendMessage(pid, cid, { ...body, client_message_id: localId });
      lastSent.current = { cid, text: (body.text || '').trim() };
      if (local) conv.dispatch({ type: 'local_ack', id: localId, messageId: r.message_id });
      if (r.delivery === 'queued') toast('已排队', 'clock');
      return true;
    } catch (e) {
      if (local) conv.dispatch({ type: 'local_drop', id: localId });
      fail(e);
      return false;
    }
  }, false);
  sendRef.current = send;

  const sug = pickSuggestions(state.suggestions, suggestions);
  const sugHidden = suggestionBarHidden(sug, state.session.state);
  // 会话已经停下、手里那份还是「运行中」（推送丢了）：再取一份
  const stuck = needsRefetch(sug, state.session.state);
  useEffect(() => { if (stuck) refetchSuggestions(); }, [stuck, refetchSuggestions]);
  // 对话正在跑的命令属于哪个节点（FL-01 commands）；服务端标出进行中之前，进度条先按它显示
  const runningCmd = ['running', 'connecting'].includes(state.session.state) ? lastUserText(state) : null;
  const viewFlow = useMemo(() => overlayRunning(flow, commandNode(flow, runningCmd)), [flow, runningCmd]);
  // 自主构建正在做的那一段由运行条和运行详情说明，顶栏不再放锁标记
  const lockInfo = scope && scope.state !== 'none' && !(ab.active && scope.state === 'autobuild_slice') ? readonlyInfo(scope) : null;
  const ctx = state.context;
  const ctxPct = Math.round(ctx?.pct ?? 0);
  const convTitle = convItem?.title || (plugin ? '新插件' : '对话');
  // 插件对话还没有消息时画起步卡片；有了消息，模式说明放在时间线最前面
  const hasMessages = state.order.some((k) => !['day', 'notice'].includes(state.items[k]?.kind));
  // 资料分析节点没确认：还在读资料（含还没传资料）
  const research = (flow?.nodes || []).find((n) => n.id === 'core.research');
  const researching = !!research && !['confirmed', 'skipped'].includes(research.state);
  const st0 = composerState({ sessionState: state.session.state, archived: meta?.archived, maintenance, autobuildRunning: !!state.session.autobuild || ab.running, researching: !plugin && researching });
  const st = plugin && !st0.disabled && !st0.delivery && !st0.warn ? { ...st0, placeholder: '描述要做的插件…' } : st0;
  const wbCtx = useMemo(() => {
    const entries = {};
    state.order.forEach((k) => {
      const it = state.items[k];
      (it.entry_links || it.card?.entry_links || []).forEach((r) => { entries[r.name] = r; });
      if (it.card?.entry) entries[it.card.entry.name] = it.card.entry;
    });
    return { pid, cid, entries, plugin };
  }, [pid, cid, plugin, state.order, state.items]);

  const head = (
    <div className="wx-head" ref={headRef}>
      <div className="wx-sess" title={plugin ? '切换插件' : '切换会话'} onClick={() => setMenu(menu === 'conv' ? null : 'conv')}>
        <Icon name={plugin ? 'puzzle' : 'msg'} size={14} /><span className="nm ell">{convTitle}</span><Icon name="chevd" size={12} />
      </div>
      {plugin ? <PluginTrack draft={boundDraft} /> : viewFlow ? <NavTrack flow={viewFlow} pages={extensions?.pages} onOpen={project.reloadFlow} onAct={onNav} readOnly={st.disabled} /> : <span />}
      <div className="wx-acts">
        {ab.active ? null : <StateChip session={state.session} queue={state.queue} />}
        {/* 插件对话的顶栏只留上下文和更多：待处理属于建模；新资料由导航栏上出现的「增量更新」表达 */}
        {plugin ? null : lockInfo ? (
          <span
            className="wx-ib nt"
            data-pop={lockPop(lockInfo, scope)}
            onClick={() => { if (scope.state === 'pending_confirm' && scope.changeset_id) rounds.current.get(scope.changeset_id)?.(); }}
          >
            <Icon name="lock" size={14} />
          </span>
        ) : null}
        {plugin ? null : (
          <>
            <span className="wx-ib" onClick={() => navigate(pagePath('inbox', pid))}><Icon name="inbox" size={14} />待处理{inboxCount ? <span className="n">{inboxCount}</span> : null}</span>
            <span className="wx-sep" />
          </>
        )}
        <span className="wx-ib" data-pop={ctx ? `上下文 ${Math.round(ctx.used / 1000)}k / ${Math.round(ctx.window / 1000)}k` : '上下文占用'} onClick={() => setMenu(menu === 'ctx' ? null : 'ctx')}>
          <Ring p={ctxPct} r={7} color={ctxPct >= 75 ? '#F59E0B' : '#2563EB'} /><span className="num">{ctxPct}%</span>
        </span>
        <span className="wx-ib" title="更多" onClick={() => setMenu(menu === 'more' ? null : 'more')}><Icon name="more" size={16} /></span>
      </div>
      {menu === 'conv' ? <ConversationMenu items={menuConversations(list?.items, plugin)} plugin={plugin} cid={cid} running={['running', 'connecting', 'queued'].includes(state.session.state)} ops={convOps} onClose={() => setMenu(null)} /> : null}
      {menu === 'ctx' ? <div className="wb-pop wb-ctx-pop"><ContextPanel context={ctx} usage={usage} /></div> : null}
      {menu === 'more' ? (
        <div className="menu wb-pop wb-more-menu">
          <div className="mi" onClick={() => { setMenu(null); setRestart(true); }}><Icon name="refresh" size={14} /><span>重启会话</span></div>
          <div className="mi" onClick={() => setMenu('ctx')}><Icon name="gauge" size={14} /><span>上下文与用量</span></div>
        </div>
      ) : null}
    </div>
  );

  return (
    <ShellP active="wb-l2" flush nosub>
      <ActionsCtx.Provider value={actions}>
        <PluginCtx.Provider value={pluginCtx}>
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
                {inc.hideTimeline ? null : <EarlierHistory older={conv.older} onLoad={onLoadEarlier} rootRef={mainRef} />}
                {plugin && state.loaded && !hasMessages ? <PluginStart onPick={(t) => comp.current.setText?.(t)} /> : null}
                {inc.hideTimeline ? null : <Timeline state={state} scope={scope} lead={plugin && hasMessages && !conv.older?.hasMore ? <PluginModeNote /> : null} />}
                {state.session.state === 'queued' && state.queue ? <QueueCard queue={state.queue} onCancel={() => api.cancelQueue(pid, cid).catch(fail)} /> : null}
              </div>
            </div>
            <div className="wbc-foot">
              <button type="button" className={cx('wb-tobottom', away && 'on')} title="回到底部" onClick={toBottom}><Icon name="chevd" size={16} /></button>
              <div className="in">
                {/* 自主构建的运行对话由运行条说明在做什么、控制暂停停下 */}
                {ab.running ? null : <RunLine info={runStatus(state)} />}
                <NextBar next={state.next} running={running} onStart={() => nextOp('start')} onCancel={() => nextOp('cancel')} />
                {/* 插件对话里没有建议条（F17 第 9 条） */}
                {sugHidden || plugin ? null : (
                  <SuggestionBar
                    sug={sug} quick={quickActions} onAction={onSuggestion} onQuick={runQuick} onAll={() => comp.current.setText?.('/')}
                    lead={returnId ? <span className="s" onClick={() => enterPlugin(returnId)}><Icon name="puzzle" size={12} />回到插件开发</span> : null}
                  />
                )}
                <Composer pid={pid} apiRef={comp} st={st} buildMode={modeSw.mode} modeLabel={plugin ? '插件开发' : modeSw.label} onMode={onMode} onSend={send} onCommand={sendCommand} onStop={canInterrupt(running, ab) ? interrupt : null} plugin={plugin} />
              </div>
            </div>
            {drag ? <div className="wb-dropmask"><Icon name="upload" size={24} /><b>松开以添加附件</b><span className="xs muted">Markdown、Word、Excel、PDF、Drawio、TXT · 单个不超过 20 MB</span></div> : null}
          </div>
          <Modal
            open={restart} onClose={() => setRestart(false)} icon="refresh" title="重启会话？" w={460}
            foot={<><Button onClick={() => setRestart(false)}>取消</Button><Button kind="primary" onClick={async () => { setRestart(false); try { await api.restartConversation(pid, cid); toast('会话已重启', 'check'); } catch (e) { fail(e); } }}>重启会话</Button></>}
          >
            <div className="sm muted">对话记录保留；稳定引擎会清空模型上下文</div>
          </Modal>
          <Modal
            open={!!replaceAsk} onClose={() => setReplaceAsk(null)} icon="clock" title={`替换排队中的「${replaceAsk?.label || '命令'}」？`} w={440}
            foot={<><Button onClick={() => setReplaceAsk(null)}>取消</Button><Button kind="primary" onClick={() => { const b = replaceAsk.body; setReplaceAsk(null); sendCommand(b, true); }}>替换</Button></>}
          >
            <div className="sm muted">这一轮结束后改做「{replaceAsk?.body?.text || replaceAsk?.body?.command}」</div>
          </Modal>
          <DraftFilesDrawer open={!!filesDrawer?.open} name={filesDrawer?.name} changes={filesDrawer?.changes} onClose={() => setFilesDrawer((f) => (f ? { ...f, open: false } : f))} />
          {opened ? (
            <Suspense fallback={null}>
              <ArtifactOpener artifact={opened} onClose={closeOpened} />
            </Suspense>
          ) : null}
          {mDialog ? <Suspense fallback={null}><MaterialsDialogs dialog={mDialog} onClose={closeMDialog} /></Suspense> : null}
          {inc.element}
        </WbCtx.Provider>
        </AutobuildCtx.Provider>
        </PluginCtx.Provider>
      </ActionsCtx.Provider>
    </ShellP>
  );
}
