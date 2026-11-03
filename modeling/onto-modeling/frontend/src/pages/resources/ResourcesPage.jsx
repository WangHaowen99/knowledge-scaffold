// 资料区（设计稿 resources）：资料列表与用途（R-01、R-03）、上传与替换（U-01 + R-02，可标为修订资料）、删除（R-04、R-05）、
// 外部反馈分组（R-11、R-12，反馈演进包）、右侧预览（ResourcePreview.jsx）。
// 别处跳来：?anchor=<file:… 锚点>（查看原文，定位并高亮那一段）、?path=<资料路径>（上传文本版）、?group=feedback&feedback=<id>（逐条查看）。
// 页面只发命令（S-20）：改用途后的重读、修订资料的增量更新、把资料读完，进度条显示在页头下。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { finishedTasks, mergeTask, pageTask } from '../../state/commandTask.js';
import { Button, Check, Chip, Empty, Icon, Modal, Note, Prog, cx } from '../../ui/index.jsx';
import { CommandRunBar, useCommandTasks, useProjectEvent } from '../plugin/parts.jsx';
import { FeedbackPreview, MaterialPreview, useFullscreen } from './ResourcePreview.jsx';
import {
  PURPOSE_FALLBACK, UPLOAD_ACCEPT, checkUpload, extOf, feedbackView, fileIcon, findMaterial, fmtSize, materialTitle, metaText, sortMaterials,
  pickDefault, purposeView, refSentence, replaceName, revisionBanner, routeTarget, targetKey, textChip,
} from './resourcesModel.js';
import '../../styles/evidence.css';
import '../../styles/resources.css';

const PAGE = 'resources';

// R-02 / R-03 返回的任务（修订资料的增量更新、改用途后的重读）：按 task_id 跟项目流 command.progress
function useTrackedTasks(onFinish) {
  const [list, setList] = useState([]);
  const ref = useRef([]);
  const finishRef = useRef(onFinish);
  finishRef.current = onFinish;
  const apply = useCallback((fn) => {
    const prev = ref.current;
    const next = fn(prev);
    ref.current = next;
    setList(next);
    finishedTasks(prev, next).forEach((t) => finishRef.current?.(t));
  }, []);
  useProjectEvent('command.progress', (task) => {
    if (ref.current.some((t) => t.task_id === task.task_id)) apply((prev) => mergeTask(prev, task));
  });
  const track = useCallback((task) => { if (task?.task_id) apply((prev) => mergeTask(prev, task)); }, [apply]);
  return { list, track };
}

// 用途菜单（设计稿 .evd-pmenu）：点菜单外、或再点一次用途按钮关掉
function PurposeMenu({ value, auto, purposes, onPick, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const h = (e) => { if (!ref.current?.contains(e.target) && !e.target.closest?.('[data-purp-open]')) onClose(); };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, [onClose]);
  return (
    <div className="evd-pmenu" ref={ref} onClick={(e) => e.stopPropagation()}>
      <div className="menu">
        {purposes.map((x) => (
          <div key={x.value} className="mi" onClick={() => onPick(x.value)}>
            {x.value === value ? <Icon name="check" size={14} /> : <span style={{ width: 14, flex: 'none' }} />}
            <div>
              <div className="pt" style={x.value === value ? { color: 'var(--blue2)', fontWeight: 600 } : undefined}>
                {x.label}{auto && x.value === value ? <span className="auto">自动识别</span> : null}
              </div>
              {x.value !== 'auto' && x.description ? <small>{x.description}</small> : null}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function FileRow({ m, picked, purposes, menuOpen, onPick, onMenu, onPurpose, onReplace, onDelete, onRevision }) {
  const ic = fileIcon(m.kind);
  const p = purposeView(m, purposes);
  const tc = textChip(m);
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return (
    <div className={cx('evd-f', picked && 'pick')} data-path={m.path} onClick={() => onPick(m)}>
      <span className={cx('fi', ic.cls)}><Icon name={ic.icon} size={14} /></span>
      <div style={{ minWidth: 0 }}>
        <div className="row g4">
          <span className="b ell" title={materialTitle(m)}>{materialTitle(m)}</span>
          {m.is_revision ? <Chip tone="amber" sm icon="refresh">修订资料</Chip> : null}
          {tc ? <Chip tone={tc.tone} sm icon={tc.icon} title={tc.title} data-text-state={m.text.state}>{tc.text}</Chip> : null}
        </div>
        <div className="xs muted ell">
          {metaText(m)}
          {m.is_revision && m.revision_task ? <> · <span className="evd-incl" onClick={stop(() => onRevision(m))}>去对话里确认 →</span></> : null}
        </div>
      </div>
      <div className="evd-purp" data-purp {...(menuOpen ? { 'data-purp-open': '' } : {})} onClick={stop(() => onMenu(menuOpen ? null : m.path))}>
        {p.label}<Icon name="chevd" size={12} />
      </div>
      <div className="evd-acts">
        <Button size="sm" kind="ghost" icon="eye" title="预览" onClick={stop(() => onPick(m, true))} />
        <Button size="sm" kind="ghost" icon="upload" title="替换" onClick={stop(() => onReplace(m))} />
        <Button size="sm" kind="ghost" icon="trash" title="删除" onClick={stop(() => onDelete(m))} />
      </div>
      {menuOpen ? <PurposeMenu value={m.purpose} auto={p.auto} purposes={purposes} onPick={(v) => onPurpose(m, v)} onClose={() => onMenu(null)} /> : null}
    </div>
  );
}

function UploadRow({ u, onDismiss }) {
  if (u.state === 'error') {
    return (
      <div className="evd-up" style={{ background: 'var(--r50)', borderColor: 'var(--r100)' }}>
        <Icon name="alert" size={14} />
        <span className="b">{u.name}</span>
        <span className="sm" style={{ color: 'var(--red)' }}>{u.error}</span>
        <span className="sp" />
        <Button size="xs" kind="ghost" icon="x" title="关闭" onClick={onDismiss} />
      </div>
    );
  }
  return (
    <div className="evd-up">
      <Icon name="filetext" size={14} />
      <span className="b">{u.name}</span>
      <span className="muted xs">{fmtSize(u.size)}</span>
      <div className="grow"><Prog p={u.pct} thin /></div>
      <span className="xs muted num">{Math.round(u.pct)}%</span>
      <Button size="xs" kind="ghost" icon="x" title="取消上传" onClick={onDismiss} />
    </div>
  );
}

function FeedbackRow({ fb, picked, onPick, onHandle, onSetState }) {
  const { pid } = useProject();
  const v = feedbackView(fb);
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  let link = null;
  if (fb.state === 'open') link = <span className="evd-incl" onClick={stop(onHandle)}>在对话里处理 →</span>;
  else if (fb.state === 'handled' && fb.handled_version_label) link = <span className="evd-incl" onClick={stop(() => navigate(pagePath('versions', pid), { v: fb.handled_version_label }))}>看对应版本 →</span>;
  else link = <span className="evd-incl" onClick={stop(() => onSetState('open'))}>重新打开</span>;
  return (
    <div className={cx('evd-fb', 'rs-fb', picked && 'pick')} data-feedback={fb.id} onClick={() => onPick(fb)}>
      <span
        className="fi"
        style={{
          width: 30, height: 30, borderRadius: 9, display: 'grid', placeItems: 'center', flex: 'none',
          background: v.open ? 'var(--a50)' : 'var(--panel3)', color: v.open ? 'var(--amber2)' : 'var(--ink3)',
        }}
      >
        <Icon name={v.icon} size={14} />
      </span>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="b ell">{v.title}</div>
        <div className="xs muted ell">{v.meta}</div>
      </div>
      <Chip tone={v.state[1]} sm icon={v.state[2]}>{v.state[0]}</Chip>
      {link}
    </div>
  );
}

// 删除弹窗（设计稿 evdDel）：先查被谁引用（R-05），再删（R-04）
function DeleteDialog({ m, onClose, onDeleted }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [refs, setRefs] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!m) return undefined;
    let alive = true;
    setRefs(null);
    api.resourceReferences(pid, m.path).then((r) => alive && setRefs(r.items || [])).catch(() => alive && setRefs([]));
    return () => { alive = false; };
  }, [api, pid, m]);
  if (!m) return null;
  const del = async () => {
    setBusy(true);
    try {
      const r = await api.deleteResource(pid, m.path);
      toast(`已删除，保存为版本 ${r.version_label}`, 'trash');
      onDeleted(m.path);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open onClose={onClose} danger icon="trash" title={`删除 ${materialTitle(m)}？`} sub="可以从版本历史恢复"
      foot={<><Button onClick={onClose}>取消</Button><Button kind="danger" className="fill" icon="trash" disabled={busy} onClick={del}>删除资料</Button></>}
    >
      {refs == null ? <div className="xs muted">正在查引用…</div> : refs.length ? <Note tone="amber">{refSentence(refs)}</Note> : null}
    </Modal>
  );
}

export default function ResourcesPage({ route }) {
  const { api, services } = useApp();
  const { pid } = useProject();
  const [data, setData] = useState(null);
  const [feedback, setFeedback] = useState([]);
  const [sel, setSel] = useState(null); // {kind: 'file', path, fragment} | {kind: 'feedback', id}
  const [menu, setMenu] = useState(null);
  const [del, setDel] = useState(null);
  const [rev, setRev] = useState(false);
  const [ups, setUps] = useState([]);
  const [over, setOver] = useState(false);
  const [fbBusy, setFbBusy] = useState(false);
  const [full, toggleFull] = useFullscreen();
  const inputRef = useRef(null);
  const pickMode = useRef(null); // 文件选择框的用途：null 上传 / {replaceOf} 替换
  const cancelled = useRef(new Set());
  const listRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const r = await api.resources(pid);
      setData(r);
      if (r.feedback_group?.enabled) {
        const f = await api.feedback(pid).catch(() => ({ items: [] }));
        setFeedback(f.items || []);
      } else setFeedback([]);
    } catch (e) {
      setData((d) => d || { error: errorText(e) });
    }
  }, [api, pid]);
  useEffect(() => { load(); }, [load]);

  const onTaskDone = useCallback((t) => {
    load();
    const s = t.result?.summary;
    toast(s && s !== '已完成' ? s : `做完了：${String(t.title || '').replace(/^助手正在/, '')}`, t.state === 'done' ? 'check' : 'alert');
  }, [load]);
  const tasks = useCommandTasks(PAGE, { onFinish: onTaskDone });
  const tracked = useTrackedTasks(onTaskDone);
  const running = pageTask([...tasks.tasks, ...tracked.list]);

  // 资料有改动（上传、替换、删除、改用途、资料分析读到新位置）、扩展项变化（反馈分组）时重拉列表
  useProjectEvent(['workspace.changed', 'extensions.updated', 'materials.updated'], (d, env) => {
    if (env.type === 'workspace.changed' && (d.paths || []).length && !d.paths.some((x) => x.startsWith('resource/'))) return;
    load();
  });
  // 文本版状态（EP-25）：转换中、等待、失败这些不出提交，只改那一份的状态
  useProjectEvent('materials.text.updated', (d) => {
    if (!d?.path) return;
    setData((cur) => (cur?.groups ? {
      ...cur,
      groups: cur.groups.map((g) => ({ ...g, items: (g.items || []).map((x) => (x.path === d.path ? { ...x, text: d.text } : x)) })),
    } : cur));
  });

  // 别处跳来：按地址参数选中资料或反馈，并把那一行滚进视野
  const target = routeTarget(route?.query || {});
  const tkey = targetKey(target);
  useEffect(() => { if (target) setSel(target); }, [tkey]); // eslint-disable-line react-hooks/exhaustive-deps
  const scrolledFor = useRef('');
  useEffect(() => {
    if (!data || !tkey || scrolledFor.current === tkey) return;
    scrolledFor.current = tkey;
    const root = listRef.current;
    const el = target.kind === 'feedback'
      ? (target.id && root?.querySelector(`[data-feedback="${CSS.escape(target.id)}"]`)) || root?.querySelector('.rs-fbgrp')
      : root?.querySelector(`[data-path="${CSS.escape(target.path)}"]`);
    el?.scrollIntoView({ block: 'center' });
  }, [data, tkey, feedback]); // eslint-disable-line react-hooks/exhaustive-deps

  // 不按工作区目录分组（F4b-paths）：一张表按资料名排，类型写在第二行
  const materials = useMemo(() => sortMaterials(data?.groups), [data]);
  const purposes = data?.purposes?.length ? data.purposes : PURPOSE_FALLBACK;
  const fbOn = !!data?.feedback_group?.enabled;
  const selFeedback = sel?.kind === 'feedback' ? feedback.find((x) => x.id === sel.id) || (sel.id ? null : feedback[0]) : null;
  const selPath = sel?.kind === 'file' ? sel.path : sel?.kind === 'feedback' ? null : pickDefault(data?.groups);
  const selected = selPath ? findMaterial(data?.groups, selPath) : null;
  const banner = revisionBanner(data?.groups);
  const total = materials.length;

  const pickFile = (m, toPreview) => {
    setSel({ kind: 'file', path: m.path, fragment: null, n: toPreview ? Date.now() : undefined });
    setMenu(null);
  };
  const goIncremental = (m) => navigate(pagePath('incremental', pid), m.revision_task?.conversation_id ? { c: m.revision_task.conversation_id } : undefined);

  // ---------- 上传 ----------
  const patchUp = (key, patch) => setUps((u) => u.map((x) => (x.key === key ? { ...x, ...patch } : x)));
  const upload = async (files, { replaceOf = null } = {}) => {
    const list = [...(files || [])];
    if (!list.length) return;
    const revision = !replaceOf && rev;
    const batch = revision ? `batch-${Date.now().toString(36)}` : undefined;
    // 先在本地挡掉格式不对、太大的（立刻显示红条），其余按顺序上传
    const rows = [];
    list.forEach((raw) => {
      let file = raw;
      if (replaceOf) {
        const r = replaceName(replaceOf, raw.name);
        if (r.error) { toast(r.error, 'alert'); return; }
        file = new File([raw], r.name, { type: raw.type });
      }
      rows.push({ key: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, file, bad: checkUpload(file.name, file.size) });
    });
    setUps((u) => [...u, ...rows.map(({ key, file, bad }) => (bad
      ? { key, name: file.name, size: file.size, state: 'error', error: bad }
      : { key, name: file.name, size: file.size, pct: 0, state: 'up' }))]);
    const done = [];
    for (const { key, file } of rows.filter((x) => !x.bad)) {
      try {
        const up = await services.upload(file, 'material', { projectId: pid, onProgress: (p) => patchUp(key, { pct: p }) });
        if (cancelled.current.has(key)) { api.deleteUpload(up.upload_id).catch(() => {}); continue; }
        const r = await api.addResources(pid, { upload_id: up.upload_id, ...(revision ? { is_revision: true, batch_id: batch } : {}) });
        setUps((u) => u.filter((x) => x.key !== key));
        done.push(r);
        if (r.command_task) tracked.track(r.command_task);
      } catch (e) {
        if (!cancelled.current.has(key)) patchUp(key, { state: 'error', error: errorText(e) });
      }
    }
    if (!done.length) return;
    const last = done[done.length - 1];
    if (replaceOf) toast(`已替换 ${materialTitle(replaceOf)}，保存为新版本 ${last.version_label}`, 'upload');
    else toast(`已上传 ${done.length} 份资料（${last.version_label}）${revision ? '；已发起增量更新' : ''}`, 'upload');
    if (revision) setRev(false);
    setSel({ kind: 'file', path: last.material.path, fragment: null });
    load();
  };
  const choose = (mode = null) => {
    pickMode.current = mode;
    const input = inputRef.current;
    if (!input) return;
    input.accept = mode?.replaceOf ? `.${extOf(mode.replaceOf.name)}` : mode?.textOnly ? '.docx,.doc,.txt,.md' : UPLOAD_ACCEPT;
    input.multiple = !mode?.replaceOf;
    input.value = '';
    input.click();
  };
  const dismissUp = (u) => {
    if (u.state === 'up') cancelled.current.add(u.key);
    setUps((list) => list.filter((x) => x.key !== u.key));
  };

  // ---------- 改用途（R-03）：资料分析开始过时，服务端顺带发一条重读命令 ----------
  const setPurpose = async (m, value) => {
    setMenu(null);
    if (value === m.purpose && !m.purpose_auto) return;
    try {
      const r = await api.setResourcePurpose(pid, { path: m.path, purpose: value });
      setData((d) => ({
        ...d,
        groups: d.groups.map((g) => ({ ...g, items: g.items.map((x) => (x.path === m.path ? { ...x, ...r.material, purpose: value, purpose_auto: false } : x)) })),
      }));
      if (r.reread_task) tracked.track(r.reread_task);
      const label = purposes.find((x) => x.value === value)?.label || value;
      toast(`用途已改为「${label}」`, 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  // ---------- 外部反馈（R-12 标状态；在对话里处理走 S-20） ----------
  const setFbState = async (fb, state) => {
    setFbBusy(true);
    try {
      const r = await api.setFeedbackState(pid, fb.id, { state });
      setFeedback((list) => list.map((x) => (x.id === fb.id ? r : x)));
      toast({ handled: '已标为已处理', dismissed: '已忽略这条反馈', open: '已重新打开' }[state], 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setFbBusy(false);
    }
  };
  const handleFb = (fb) => tasks.send({ command: '/ontology-evolution', args: { feedback_id: fb.id }, text: `处理外部反馈：${feedbackView(fb).title}` });

  // ---------- 右侧 ----------
  let right;
  if (sel?.kind === 'feedback' && selFeedback) {
    right = <FeedbackPreview fb={selFeedback} busy={fbBusy || tasks.sending} onSetState={(s) => setFbState(selFeedback, s)} onHandle={() => handleFb(selFeedback)} />;
  } else if (selected) {
    right = (
      <MaterialPreview
        key={`${selected.path}:${sel?.n || ''}`} m={selected} purposes={purposes} fragment={sel?.kind === 'file' ? sel.fragment : null}
        full={full} onFull={toggleFull} onSaved={load} onUploadText={() => choose({ textOnly: true })} tasks={tasks}
      />
    );
  } else if (data && !data.error && selPath) {
    right = (
      <div className="card">
        <Empty icon="filetext" title="找不到这份资料" actions={<Button size="sm" icon="history" onClick={() => navigate(pagePath('versions', pid))}>去版本历史</Button>}>
          「{selPath.split('/').pop()}」可能已被删除。
        </Empty>
      </div>
    );
  }

  return (
    <ShellP active={PAGE}>
      <PageHead
        title="资料区"
        actions={<Button kind="primary" icon="upload" onClick={() => choose()}>上传资料</Button>}
      />
      {banner ? (
        <div className="evd-banner">
          <span className="ic"><Icon name="refresh" size={14} /></span>
          <div className="grow"><b>{materialTitle(banner)}</b><span className="muted">：{banner.revision_task.label}</span></div>
          <Button size="sm" kind="primary" icon="arrow" onClick={() => goIncremental(banner)}>去对话里确认</Button>
        </div>
      ) : null}
      <CommandRunBar task={running} className="rs-run" />
      <div className="evd-rg">
        <div ref={listRef}>
          <div
            className={cx('evd-drop', over && 'rs-over')}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}
          >
            <span className="ic"><Icon name="upload" size={20} /></span>
            <div className="grow">
              <div className="b lg">把资料拖到这里</div>
              <div className="muted sm">Word、Excel、PDF、Markdown、CSV、drawio、TXT、HTML · ≤ 20 MB</div>
            </div>
            <Button icon="folder" onClick={() => choose()}>选择文件</Button>
          </div>
          <div className="evd-rev">
            <Check on={rev} onChange={setRev} label={<>作为<b>修订资料</b>上传（发起增量更新）</>} />
          </div>
          {ups.length ? <div className="col mt8" style={{ gap: 6 }}>{ups.map((u) => <UploadRow key={u.key} u={u} onDismiss={() => dismissUp(u)} />)}</div> : null}

          {!data ? <div className="evd-grp muted sm"><span className="spin" /> 正在读取资料…</div> : null}
          {data?.error ? <Note tone="red" className="mt16">{data.error}<div className="row mt8"><Button size="xs" icon="refresh" onClick={load}>重试</Button></div></Note> : null}
          {data && !data.error && !total ? (
            <div className="card evd-grp">
              <Empty icon="folder" title="还没有资料">把场景说明、业务规则、样例单据和数据表拖进来。</Empty>
            </div>
          ) : null}

          {total ? (
            <div className="evd-grp">
              <div className="card">
                {materials.map((m) => (
                  <FileRow
                    key={m.path} m={m} picked={!!selected && selected.path === m.path && sel?.kind !== 'feedback'} purposes={purposes} menuOpen={menu === m.path}
                    onPick={pickFile} onMenu={setMenu} onPurpose={setPurpose} onReplace={(x) => choose({ replaceOf: x })} onDelete={setDel} onRevision={goIncremental}
                  />
                ))}
              </div>
            </div>
          ) : null}

          {fbOn ? (
            <div className="evd-grp rs-fbgrp">
              <h4>
                <Icon name="msg" size={14} />外部反馈<span className="faint" style={{ fontWeight: 500 }}>{feedback.length}</span><span className="sp" />
                <span className="evd-plg" data-pop="由「反馈演进」插件提供" onClick={() => navigate(pagePath('plugin-detail'), { pack: 'evo' })}>
                  <Icon name="puzzle" size={12} />
                </span>
              </h4>
              <div className="card">
                {feedback.length ? feedback.map((fb) => (
                  <FeedbackRow
                    key={fb.id} fb={fb} picked={selFeedback?.id === fb.id} onPick={(x) => setSel({ kind: 'feedback', id: x.id })}
                    onHandle={() => handleFb(fb)} onSetState={(s) => setFbState(fb, s)}
                  />
                )) : <div className="evd-fb xs muted">还没有外部反馈</div>}
              </div>
            </div>
          ) : null}
        </div>

        <div className={cx('evd-prev', full && 'rs-full')} onClick={full ? (e) => { if (e.target === e.currentTarget) toggleFull(); } : undefined}>{right}</div>
      </div>

      <input
        ref={inputRef} type="file" hidden
        onChange={(e) => { const mode = pickMode.current; upload(e.target.files, mode?.replaceOf ? { replaceOf: mode.replaceOf } : {}); }}
      />
      <DeleteDialog
        m={del} onClose={() => setDel(null)}
        onDeleted={(path) => { setDel(null); if (selPath === path) setSel(null); load(); }}
      />
    </ShellP>
  );
}
