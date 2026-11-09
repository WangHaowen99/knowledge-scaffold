// 工作台插件开发模式（ADR-0087、契约 F17）：插件三步、起步卡片、草稿卡、文件抽屉，以及草稿状态的共用缓存
import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
} from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Drawer, Icon, cx } from '../../ui/index.jsx';
import { CC, MSys } from './parts.jsx';
import {
  CHANGE_LABEL, START_OPTIONS, canEditFile, cardDiffRows, defaultFile, diffRows, draftCardHead, draftCardMode, fixMessage, groupFiles,
  knowledgeTags, nodePlacementText, pluginSteps, precheckChip, splitPrecheck,
} from './pluginModeModel.js';
import '../../styles/plugins.css';
import '../../styles/plugin-mode.css';

/**
 * 插件对话的共用件：drafts（名称 → DraftView）、ensure / reload / put、latestCards（名称 → 最新草稿卡的键）、
 * openFiles(name, changes)、sendText、backToModel、discard
 */
export const PluginCtx = createContext(null);
const usePlugin = () => useContext(PluginCtx);
const viewOf = (v) => (v?.name ? v : null);

/** 草稿状态缓存（PD-02）：同一份草稿的几张卡、顶栏、抽屉共用一份；项目流 plugin_drafts.changed 时重取 */
export function usePluginDrafts() {
  const { api } = useApp();
  const { pid, subscribe } = useProject();
  const [drafts, setDrafts] = useState({});
  const wanted = useRef(new Set());
  const reload = useCallback((name) => {
    wanted.current.add(name);
    return api.pluginDraft(pid, name)
      .then((v) => { setDrafts((d) => ({ ...d, [name]: v })); return v; })
      .catch((e) => { setDrafts((d) => ({ ...d, [name]: viewOf(d[name]) || { error: e } })); return null; });
  }, [api, pid]);
  const ensure = useCallback((name) => { if (name && !wanted.current.has(name)) reload(name); }, [reload]);
  const put = useCallback((v) => { if (v?.name) setDrafts((d) => ({ ...d, [v.name]: v })); }, []);
  useEffect(() => subscribe((env) => {
    if (env.type !== 'plugin_drafts.changed') return;
    (env.data?.names || [...wanted.current]).filter((n) => wanted.current.has(n)).forEach(reload);
  }), [subscribe, reload]);
  return { drafts, ensure, reload, put };
}

/** 顶栏中间的插件三步 */
export function PluginTrack({ draft }) {
  const steps = pluginSteps(draft);
  return (
    <div className="wx-track pm">
      {steps.map((s, i) => (
        <span key={s.id} style={{ display: 'contents' }}>
          {i ? <span className={cx('wx-ln', steps[i - 1].st === 'ok' && 'ok')} /> : null}
          <span className={cx('wx-st', s.st)}>
            <span className="dt">{s.st === 'ok' ? <Icon name="check" /> : s.st === 'fail' ? <Icon name="x" /> : null}</span>
            {s.label}
            {s.x ? <span className="x">{s.x}</span> : null}
          </span>
        </span>
      ))}
    </div>
  );
}

export const PluginModeNote = () => <MSys icon="puzzle">已切到插件开发 · 本体只读</MSys>;

/** 插件对话还没有消息：模式说明 + 四张起步卡片，点了把一句起头填进输入框 */
export function PluginStart({ onPick }) {
  return (
    <>
      <PluginModeNote />
      <div className="pm-grid">
        {START_OPTIONS.map((o) => (
          <div key={o.id} className="pm-opt" onClick={() => onPick(o.text)}>
            <span className={cx('pl-ico', o.tone)}><Icon name={o.icon} size={18} /></span>
            <div className="b">{o.title}</div>
          </div>
        ))}
      </div>
    </>
  );
}

function Chk({ state, title, sub, tag }) {
  return (
    <div className="pl-chk">
      <span className={cx('d', state)}><Icon name={{ ok: 'check', bad: 'x', warn: 'alert' }[state]} /></span>
      <div className="grow"><div className="sm b">{title}</div>{sub ? <div className="xs muted">{sub}</div> : null}</div>
      {tag}
    </div>
  );
}

function DiffBlock({ diff, brief = false }) {
  return (
    <div className="pm-code">
      <div className="l h"><i /><span>{diff.path}</span></div>
      {(brief ? cardDiffRows(diff) : diffRows(diff)).map((r, i) => <div key={i} className={cx('l', r.kind)}><i>{r.mark}</i><span>{r.text || ' '}</span></div>)}
    </div>
  );
}

const ChipOf = ({ c }) => (c ? <Chip tone={c.tone} sm icon={c.icon}>{c.text}</Chip> : null);

// 卡片正文：启用后带来什么（流程、命令、脚本、知识）、预检、安装；改过时换成和已装版本的差异
function DraftBody({ draft, mode, failedOverride }) {
  const [showRest, setShowRest] = useState(false);
  const c = draft.contents || {};
  const pre = failedOverride ? { state: 'failed', summary: { fail: failedOverride.length }, items: failedOverride } : draft.precheck;
  const failed = failedOverride || splitPrecheck(pre?.items).failed;
  const failedIds = new Set(failed.map((i) => i.id));
  const rest = splitPrecheck(draft.precheck?.items).rest.filter((i) => !failedIds.has(i.id));
  const ins = draft.install || {};
  const outdated = ins.state === 'outdated';
  const tags = (list, mono) => list.map((t) => <span key={t} className="tag" style={mono ? undefined : { fontFamily: 'inherit' }}>{t}</span>);
  const row = (label, body) => (body ? <><dt>{label}</dt><dd className="row g6" style={{ flexWrap: 'wrap' }}>{body}</dd></> : null);
  const some = (xs) => (xs?.length ? xs : null);
  return (
    <>
      {outdated && ins.diff ? <DiffBlock diff={ins.diff} brief /> : null}
      <dl className="wx-def" style={{ margin: outdated && ins.diff ? '12px 0 0' : 0 }}>
        {outdated ? row('改动', !ins.diff && some(ins.changes) ? tags(ins.changes.map((x) => x.path), true) : null) : (
          <>
            {c.nodes?.length ? (
              <>
                <dt>流程</dt>
                <dd>{c.nodes.map((n) => <div key={n.id}><span className="wl pm-node">{n.title}</span> {nodePlacementText(n)}</div>)}</dd>
              </>
            ) : null}
            {row('命令', some(c.commands) && tags(c.commands.map((x) => x.label || x.name)))}
            {row('技能', some(c.skills) && tags(c.skills))}
            {row('子助手', some(c.agents) && tags(c.agents))}
            {row('脚本', some(c.scripts) && tags(c.scripts, true))}
            {row('知识', some(knowledgeTags(c.knowledge)) && tags(knowledgeTags(c.knowledge)))}
          </>
        )}
        {row('预检', pre ? <ChipOf c={precheckChip(pre)} /> : null)}
        {mode === 'installed' ? row('安装', <span>已启用到本项目</span>) : null}
      </dl>
      {failed.length && mode !== 'snapshot' ? (
        <div className="pm-chk">
          {failed.map((i) => <Chk key={i.id || i.label} state="bad" title={i.label} sub={i.detail} tag={<Chip tone="red" sm>没过</Chip>} />)}
          {rest.length ? (
            <Chk
              state="ok"
              title={`其余 ${rest.length} 项通过`}
              tag={<span className="xs muted more" onClick={() => setShowRest(!showRest)}>{showRest ? '收起' : '展开'}</span>}
            />
          ) : null}
          {showRest ? rest.map((i) => <Chk key={i.id || i.label} state={i.result === 'warn' ? 'warn' : 'ok'} title={i.label} sub={i.detail} />) : null}
        </div>
      ) : null}
    </>
  );
}

// 安装时静态检查没过（422）：F17 写的是 details.items，上传接口的同名错误给的是 failed（名称列表），两种都认
const installFailures = (e) => (e.details?.items || (e.details?.failed || []).map((label) => ({ id: label, label })))
  .map((i) => ({ ...i, result: 'fail' }));

/** 插件对话里的草稿卡（card.kind / card_type === 'plugin_draft'）。静态快照来自卡片，实时状态按 PD-02；只有最新的一张出按钮 */
export function PluginDraftCard({ card, cardKey }) {
  const { api } = useApp();
  const { pid } = useProject();
  const pm = usePlugin();
  const { name } = card;
  useEffect(() => { pm?.ensure(name); }, [pm?.ensure, name]); // eslint-disable-line react-hooks/exhaustive-deps
  const draft = viewOf(pm?.drafts[name]);
  const latest = !!pm && pm.latestCards[name] === cardKey;
  const [installFailed, setInstallFailed] = useState(null);
  const [busy, setBusy] = useState(null);
  const [csState, setCsState] = useState(null);
  // 草稿又变了（新的一轮、网页上改、装了）：上次安装的失败项作废
  useEffect(() => setInstallFailed(null), [draft?.updated_at]);
  // 撤回这一轮：只在最新卡、这一轮的变更集还是合入状态、草稿和装上的版本不一样时出现（装好以后撤回只会让草稿和插件对不上）
  useEffect(() => {
    if (!latest || !card.changeset_id) return undefined;
    let alive = true;
    api.changeset(pid, card.changeset_id).then((r) => alive && setCsState(r.state)).catch(() => alive && setCsState(null));
    return () => { alive = false; };
  }, [api, pid, latest, card.changeset_id, draft?.updated_at]);

  const mode = draftCardMode(draft, { latest, installFailed });
  const head = draftCardHead(draft, card, mode);
  const run = async (key, fn) => {
    setBusy(key);
    try { await fn(); } catch (e) { toast(errorText(e), 'alert'); } finally { setBusy(null); }
  };
  const install = () => run('install', async () => {
    try {
      const r = await api.installPluginDraft(pid, name);
      pm.put(r.draft);
      setInstallFailed(null);
      toast(mode === 'update' ? `已更新到 ${r.version}` : `已装好 ${r.version}`, 'check');
    } catch (e) {
      if (e.code !== 'static_check_failed') throw e;
      setInstallFailed(installFailures(e));
    }
  });
  const disable = () => run('disable', async () => {
    await api.disablePack(pid, name, {});
    toast('已停用', 'check');
    pm.reload(name);
  });
  const undo = () => run('undo', async () => {
    await pm.discard(card.changeset_id);
    const r = await api.changeset(pid, card.changeset_id).catch(() => null);
    setCsState(r?.state || null);
  });
  const fixItems = installFailed || splitPrecheck(draft?.precheck?.items).failed;
  // 进行中：按钮的图标换成转圈
  const spinner = (key) => (busy === key ? <span className="spin" style={{ width: 12, height: 12 }} /> : null);

  let foot = null;
  if (mode !== 'snapshot') {
    const undoBtn = card.changeset_id && csState === 'merged' && draft?.install?.state !== 'installed'
      ? <Button size="xs" kind="ghost" icon="undo" disabled={!!busy} onClick={undo}>撤回这一轮</Button> : null;
    const btns = {
      install: <Button size="sm" kind="primary" icon={busy === 'install' ? null : 'download'} disabled={!!busy} onClick={install}>{spinner('install')}安装并启用到本项目</Button>,
      fix: (
        <>
          <Button size="sm" icon="download" disabled>安装并启用到本项目</Button>
          <Button size="sm" kind="primary" icon="wand" disabled={!!busy} onClick={() => pm.sendText(fixMessage(fixItems))}>让助手修复</Button>
        </>
      ),
      installed: (
        <>
          <Button size="sm" kind="ghost" disabled={!!busy} onClick={disable}>{spinner('disable')}停用</Button>
          <Button size="sm" kind="primary" icon="arrowl" disabled={!!busy} onClick={() => pm.backToModel()}>切回建模试用</Button>
        </>
      ),
      update: <Button size="sm" kind="primary" icon={busy === 'install' ? null : 'refresh'} disabled={!!busy} onClick={install}>{spinner('install')}更新到本项目</Button>,
    }[mode];
    foot = <>{undoBtn}<span className="sp" />{btns}</>;
  }
  const outdated = draft?.install?.state === 'outdated';
  const acts = mode === 'snapshot' ? null : (
    <Button size="sm" kind="ghost" icon={outdated ? 'split' : 'file'} onClick={() => pm.openFiles(name, outdated)}>{outdated ? '看改动' : '查看文件'}</Button>
  );
  return (
    <CC icon="puzzle" tone="indigo" eyebrow={head.eyebrow} title={head.title} chip={<ChipOf c={head.chip} />} sub={head.sub} acts={acts} foot={foot}>
      {mode === 'snapshot' ? null : <DraftBody draft={draft} mode={mode} failedOverride={installFailed} />}
    </CC>
  );
}

function CodeView({ text }) {
  const lines = text.replace(/\n$/, '').split('\n');
  return <div className="pm-code">{lines.map((l, i) => <div key={i} className="l"><i>{i + 1}</i><span>{l || ' '}</span></div>)}</div>;
}

/** 文件抽屉（设计稿 s4）：左边按目录分组的文件，右边带行号的只读代码；「编辑」改一个文本文件（PD-04）。changes 为真时看和已装版本的改动 */
export function DraftFilesDrawer({ open, name, changes, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const pm = usePlugin();
  const draft = name ? viewOf(pm?.drafts[name]) : null;
  const [path, setPath] = useState(null);
  const [file, setFile] = useState(null);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => { if (open && name) pm?.ensure(name); }, [open, name]); // eslint-disable-line react-hooks/exhaustive-deps
  const ready = !!draft;
  // 打开、换草稿、切到看改动时选默认文件
  useEffect(() => {
    if (open && ready) { setPath(defaultFile(draft, changes)); setEditing(null); }
  }, [open, name, changes, ready]); // eslint-disable-line react-hooks/exhaustive-deps
  const changeOf = (p) => (changes ? (draft?.install?.changes || []).find((x) => x.path === p) : null);
  useEffect(() => {
    if (!open || !name || !path) return undefined;
    let alive = true;
    setFile(null);
    if (changeOf(path)?.status === 'deleted') { setFile({ path, deleted: true }); return undefined; }
    api.pluginDraftFile(pid, name, path).then((r) => alive && setFile(r)).catch((e) => alive && setFile({ path, error: e }));
    return () => { alive = false; };
  }, [api, pid, open, name, path, tick]); // eslint-disable-line react-hooks/exhaustive-deps

  const files = [...(draft?.files || [])];
  if (changes) (draft?.install?.changes || []).filter((x) => x.status === 'deleted').forEach((x) => files.push({ path: x.path, size: 0 }));
  const groups = groupFiles(files);
  const diff = changes && draft?.install?.diff?.path === path ? draft.install.diff : null;
  const editable = canEditFile(path || '', file);
  const save = async () => {
    setSaving(true);
    try {
      await api.savePluginDraftFile(pid, name, { path, content: editing });
      setEditing(null);
      setTick((t) => t + 1);
      pm.reload(name);
      toast('已保存', 'check');
    } catch (e) {
      toast(e.code === 'session_busy' ? '助手在跑，跑完再保存' : errorText(e), 'alert');
    } finally {
      setSaving(false);
    }
  };
  const acts = editing != null ? (
    <>
      <Button size="sm" kind="ghost" disabled={saving} onClick={() => setEditing(null)}>取消</Button>
      <Button size="sm" kind="primary" icon="check" disabled={saving} onClick={save}>保存</Button>
    </>
  ) : <Button size="sm" kind="ghost" icon="edit" disabled={!editable} onClick={() => setEditing(file.content)}>编辑</Button>;

  let view = null;
  if (editing != null) view = <textarea autoFocus spellCheck={false} value={editing} onChange={(e) => setEditing(e.target.value)} />;
  else if (diff) view = <DiffBlock diff={diff} />;
  else if (!file) view = <span className="spin" />;
  else if (file.deleted) view = <div className="xs muted">已删除</div>;
  else if (file.error) view = <div className="note red"><Icon name="alert" /><div className="grow">{errorText(file.error)}</div></div>;
  else if (file.content == null) view = <div className="xs muted">{file.binary ? '二进制文件' : '文件太大，不显示'}</div>;
  else view = <CodeView text={file.content} />;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      className="pm-dw"
      icon={<span className="pl-ico node" style={{ width: 32, height: 32, borderRadius: 9 }}><Icon name="puzzle" size={16} /></span>}
      title={draft?.label || name}
      sub={draft ? `草稿 ${draft.version || ''} · ${draft.file_count ?? files.length} 个文件` : ''}
      acts={acts}
    >
      <div className="pm-dwb">
        <div className="pm-files">
          {groups.map((g) => (
            <div key={g.dir || '/'}>
              {g.dir ? <div className="g">{g.dir}</div> : null}
              {g.files.map((f) => {
                const ch = changeOf(f.path);
                return (
                  <div key={f.path} className={cx('f', f.path === path && 'on')} title={f.path} onClick={() => { if (editing == null) setPath(f.path); }}>
                    <Icon name="filetext" size={14} /><span className="ell">{f.name}</span>
                    {ch ? <span className={cx('cg', ch.status)}>{CHANGE_LABEL[ch.status]}</span> : null}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
        <div className="pm-view">
          {path ? <div className="p">{path}</div> : null}
          {view}
        </div>
      </div>
    </Drawer>
  );
}
