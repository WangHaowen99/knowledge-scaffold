// 资料区（设计稿 resources）：资料列表与用途（R-01、R-03）、上传与替换（U-01 + R-02）、删除（R-04、R-05）、
// 右侧预览（ResourcePreview.jsx）。
// 别处跳来：?anchor=<file:… 锚点>（查看原文，定位并高亮那一段）、?path=<资料路径>（上传文本版）。
// 页面只发命令（S-20）：改用途后的重读、把资料读完，进度条显示在页头下。
// 新资料不在这里发增量更新：建模之后传了资料，流程里自己出现增量更新节点（R-02、节点模型重构 3）。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { finishedTasks, mergeTask, pageTask } from '../../state/commandTask.js';
import { Button, Chip, Empty, Icon, Modal, Note, Prog, cx } from '../../ui/index.jsx';
import { legacyOfficeHint, uploadRules } from '../../lib/resourceUpload.js';
import { CommandRunBar, useCommandTasks, useProjectEvent } from '../plugin/parts.jsx';
import { MaterialPreview, useFullscreen } from './ResourcePreview.jsx';
import { useSheetScope } from './SheetScope.jsx';
import { isSchemaTable, scopeLink } from './sheetScopeModel.js';
import {
  PURPOSE_FALLBACK, checkUpload, extOf, fileIcon, findMaterial, fmtSize, materialTitle, metaText, sortMaterials,
  pickDefault, purposeView, refSentence, replaceName, routeTarget, targetKey, textChip,
} from './resourcesModel.js';
import '../../styles/evidence.css';
import '../../styles/resources.css';

const PAGE = 'resources';

// R-03 返回的任务（改用途后的重读）：按 task_id 跟项目流 command.progress
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

function FileRow({ m, picked, purposes, menuOpen, onPick, onMenu, onPurpose, onReplace, onDelete, onScope }) {
  const ic = fileIcon(m.kind);
  const p = purposeView(m, purposes);
  const tc = textChip(m);
  const scope = scopeLink(m.schema_scope);
  const cand = m.schema_candidate;
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  return (
    <div className={cx('evd-f', picked && 'pick')} data-path={m.path} onClick={() => onPick(m)}>
      <span className={cx('fi', ic.cls)}><Icon name={ic.icon} size={14} /></span>
      <div style={{ minWidth: 0 }}>
        <div className="row g4">
          <span className="b ell" title={materialTitle(m)}>{materialTitle(m)}</span>
          {tc ? <Chip tone={tc.tone} sm icon={tc.icon} title={tc.title} data-text-state={m.text.state}>{tc.text}</Chip> : null}
        </div>
        <div className="xs muted ell">
          {metaText(m)}
          {scope ? <> · <span className={cx('evd-incl', scope.danger && 'danger')} onClick={stop(() => onScope(m))}>{scope.text}</span></> : null}
          {cand ? (
            <> · <span className="evd-incl" title={`表头像数据库列名，识别出 ${cand.tables.length} 张表：${cand.tables.slice(0, 8).join('、')}`} onClick={stop(() => onPurpose(m, 'schema'))}>疑似数据表 · 设为 Schema</span></>
          ) : null}
        </div>
      </div>
      <div className="evd-purp" data-purp {...(menuOpen ? { 'data-purp-open': '' } : {})} onClick={stop(() => onMenu(menuOpen ? null : m.path))}>
        {p.label}{p.auto ? <span className="auto" title="上传时按表格结构自动识别（数据表或术语表）；改用途即覆盖">自动识别</span> : null}<Icon name="chevd" size={12} />
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
  if (u.state === 'warn') {
    return (
      <div className="evd-up" style={{ background: 'var(--a50)', borderColor: 'var(--a100)' }}>
        <Icon name="alert" size={14} />
        <span className="sm" style={{ color: 'var(--amber)' }}>{u.error}</span>
        <span className="sp" />
        <Button size="xs" kind="ghost" icon="x" title="关闭" onClick={onDismiss} />
      </div>
    );
  }
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
  const { api, services, config } = useApp();
  const rules = useMemo(() => uploadRules(config), [config]);
  const { pid } = useProject();
  const [data, setData] = useState(null);
  const [sel, setSel] = useState(null); // {kind: 'file', path, fragment}
  const [menu, setMenu] = useState(null);
  const [del, setDel] = useState(null);
  const [ups, setUps] = useState([]);
  const [over, setOver] = useState(false);
  const [full, toggleFull] = useFullscreen();
  const inputRef = useRef(null);
  const pickMode = useRef(null); // 文件选择框的用途：null 上传 / {replaceOf} 替换
  const cancelled = useRef(new Set());
  const listRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const r = await api.resources(pid);
      setData(r);
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

  // 资料有改动（上传、替换、删除、改用途、资料分析读到新位置）、扩展项变化（包启停）时重拉列表
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

  // 别处跳来：按地址参数选中资料，并把那一行滚进视野
  const target = routeTarget(route?.query || {});
  const tkey = targetKey(target);
  useEffect(() => { if (target) setSel(target); }, [tkey]); // eslint-disable-line react-hooks/exhaustive-deps
  const scrolledFor = useRef('');
  useEffect(() => {
    if (!data || !tkey || scrolledFor.current === tkey) return;
    scrolledFor.current = tkey;
    const root = listRef.current;
    const el = root?.querySelector(`[data-path="${CSS.escape(target.path)}"]`);
    el?.scrollIntoView({ block: 'center' });
  }, [data, tkey]); // eslint-disable-line react-hooks/exhaustive-deps

  // 不按工作区目录分组（F4b-paths）：一张表按资料名排，类型写在第二行
  const materials = useMemo(() => sortMaterials(data?.groups), [data]);
  const purposes = data?.purposes?.length ? data.purposes : PURPOSE_FALLBACK;
  const selPath = sel?.kind === 'file' ? sel.path : pickDefault(data?.groups);
  const selected = selPath ? findMaterial(data?.groups, selPath) : null;
  const total = materials.length;

  const pickFile = (m, toPreview) => {
    setSel({ kind: 'file', path: m.path, fragment: null, n: toPreview ? Date.now() : undefined });
    setMenu(null);
  };

  // ---------- 上传 ----------
  const patchUp = (key, patch) => setUps((u) => u.map((x) => (x.key === key ? { ...x, ...patch } : x)));
  const upload = async (files, { replaceOf = null } = {}) => {
    const list = [...(files || [])];
    if (!list.length) return;
    // 先在本地挡掉格式不对、太大的（立刻显示红条），其余按顺序上传
    const rows = [];
    list.forEach((raw) => {
      let file = raw;
      if (replaceOf) {
        const r = replaceName(replaceOf, raw.name);
        if (r.error) { toast(r.error, 'alert'); return; }
        file = new File([raw], r.name, { type: raw.type });
      }
      rows.push({ key: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, file, bad: checkUpload(file.name, file.size, rules) });
    });
    setUps((u) => [...u, ...rows.map(({ key, file, bad }) => (bad
      ? { key, name: file.name, size: file.size, state: 'error', error: bad }
      : { key, name: file.name, size: file.size, pct: 0, state: 'up' }))]);
    const done = [];
    for (const { key, file } of rows.filter((x) => !x.bad)) {
      try {
        const up = await services.upload(file, 'material', { projectId: pid, onProgress: (p) => patchUp(key, { pct: p }) });
        if (cancelled.current.has(key)) { api.deleteUpload(up.upload_id).catch(() => {}); continue; }
        const r = await api.addResources(pid, { upload_id: up.upload_id });
        setUps((u) => u.filter((x) => x.key !== key));
        done.push(r);
      } catch (e) {
        if (!cancelled.current.has(key)) patchUp(key, { state: 'error', error: errorText(e) });
      }
    }
    if (!done.length) return;
    const last = done[done.length - 1];
    // 旧版 .doc / .xls 传上去了但转不出文本：提示留在上传列表里（轻提示只有一条，会被结果盖掉）
    const hints = done.map((x) => ({ name: x.material?.name, hint: legacyOfficeHint(x.material?.name) })).filter((x) => x.hint);
    if (hints.length) {
      setUps((u) => [...u, ...hints.map((x) => ({ key: `hint-${x.name}-${Date.now()}`, name: x.name, state: 'warn', error: x.hint }))]);
    }
    // 一份时文字同 develop（已上传 / 已替换 X，识别出数据表时说明用途已设为数据 Schema）
    const one = done.length === 1 ? last : null;
    const detected = one?.detected
      ? `，识别为数据表（${one.detected.tables.length} 张），用途已设为「数据 Schema」，可在资料列表修改` : '';
    const head = one ? `${one.replaced ? '已替换' : '已上传'} ${one.material.name}${detected}` : `已上传 ${done.length} 份资料（${last.version_label}）`;
    toast(head, 'upload');
    setSel({ kind: 'file', path: last.material.path, fragment: null });
    load();
  };
  const choose = (mode = null) => {
    pickMode.current = mode;
    const input = inputRef.current;
    if (!input) return;
    input.accept = mode?.replaceOf ? `.${extOf(mode.replaceOf.name)}` : mode?.textOnly ? '.docx,.doc,.txt,.md' : rules.accept;
    input.multiple = !mode?.replaceOf;
    input.value = '';
    input.click();
  };
  const dismissUp = (u) => {
    if (u.state === 'up') cancelled.current.add(u.key);
    setUps((list) => list.filter((x) => x.key !== u.key));
  };

  // ---------- 改用途（R-03）：资料分析开始过时，服务端顺带发一条重读命令 ----------
  const applyPurpose = (m, value, r) => {
    setData((d) => ({
      ...d,
      groups: d.groups.map((g) => ({ ...g, items: g.items.map((x) => (x.path === m.path ? { ...x, ...r.material, purpose: value, purpose_auto: false } : x)) })),
    }));
    if (r.reread_task) tracked.track(r.reread_task);
  };
  // 表格改成数据 Schema 先预检表头（R-16）：没问题直接保存，有问题弹窗选 Sheet 范围
  const sheetScope = useSheetScope(pid, (r) => applyPurpose(r.material, 'schema', r));
  const setPurpose = async (m, value) => {
    setMenu(null);
    if (value === m.purpose && !m.purpose_auto) return;
    if (value === 'schema' && isSchemaTable(m.name)) { sheetScope.choose(m); return; }
    try {
      const r = await api.setResourcePurpose(pid, { path: m.path, purpose: value });
      applyPurpose(m, value, r);
      const label = purposes.find((x) => x.value === value)?.label || value;
      toast(`用途已改为「${label}」`, 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  // ---------- 右侧 ----------
  // 从别的文档的出处芯片点进来（?from=）：预览上方「返回 <文档名>」回到那里（develop 的 onBackToOrigin）
  const back = sel?.source && route?.query?.from ? (
    <Button size="sm" kind="ghost" icon="arrowl" className="rs-back" onClick={() => { window.location.hash = route.query.from; }}>
      返回 {route.query.fromLabel || ''}
    </Button>
  ) : null;
  let right;
  if (selected) {
    right = (
      <MaterialPreview
        key={`${selected.path}:${sel?.n || ''}`} m={selected} purposes={purposes} fragment={sel?.kind === 'file' ? sel.fragment : null}
        source={sel?.kind === 'file' && sel.source ? { textVersion: !!sel.textVersion } : null}
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
              <div className="muted sm">支持 Markdown、TXT、PDF、Word、Excel、CSV、Drawio、HTML 和常见图片；单个文件不超过 20 MB</div>
            </div>
            <Button icon="folder" onClick={() => choose()}>选择文件</Button>
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
                    key={m.path} m={m} picked={!!selected && selected.path === m.path} purposes={purposes} menuOpen={menu === m.path}
                    onPick={pickFile} onMenu={setMenu} onPurpose={setPurpose} onReplace={(x) => choose({ replaceOf: x })} onDelete={setDel} onScope={sheetScope.open}
                  />
                ))}
              </div>
            </div>
          ) : null}
        </div>

        <div className={cx('evd-prev', full && 'rs-full')} onClick={full ? (e) => { if (e.target === e.currentTarget) toggleFull(); } : undefined}>{back}{right}</div>
      </div>

      <input
        ref={inputRef} type="file" hidden
        onChange={(e) => { const mode = pickMode.current; upload(e.target.files, mode?.replaceOf ? { replaceOf: mode.replaceOf } : {}); }}
      />
      <DeleteDialog
        m={del} onClose={() => setDel(null)}
        onDeleted={(path) => { setDel(null); if (selPath === path) setSel(null); load(); }}
      />
      {sheetScope.dialog}
    </ShellP>
  );
}
