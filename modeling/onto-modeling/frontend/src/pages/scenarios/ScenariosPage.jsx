// 场景模拟（插件页，设计稿 scenarios）：场景列表（trace.scenario）、示例数据（trace.mock）、推演通过率。
// 插件页只发命令、不跑助手（详设 5.9.3）：批量验证、交互模拟、补齐过期场景、按场景补生成、行内 ▶ 都发 S-20，
// 页面显示进度条和「去对话里看」，跑完结果回到列表（artifacts.updated）。
// 写入范围只读两态：助手运行中 / 修改待确认时，锁里的场景只读（行上带锁，详情里编辑、删除停用）。
// 地址参数：?key=<场景 key> 定位并高亮那一行（Studio、报告页跳过来）；?tab=md 打开「示例数据」页签。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { lockState } from '../../state/readonly.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import {
  Chip, Empty, Ev, Icon, Note, Seg, Select, St, Stat, Tabs, cx,
} from '../../ui/index.jsx';
import {
  CommandRunBar, ExtraActions, PluginEyebrow, PluginOff, ScopeLockBar, SendButton, useCommandTasks, usePageEnabled, useProjectEvent,
} from '../plugin/parts.jsx';
import {
  INPUT_KIND, MOCK, SCENARIO, STATUS_SEG, batchCommand, countRows, enumLabel, enumOptions, failDesc, failHint, filterRows, showSlice,
  lastRun, lockBarText, mockCommand, mockDesc, passRate, refreshCommand, rowLocked, rowLockPop, rowState,
  skippedNote, sparkPaths, staleDesc, walkCommand, walkTarget,
} from './scenariosModel.js';
import { ScenarioDrawer } from './ScenarioDrawer.jsx';
import '../../styles/scenarios.css';

export default function ScenariosPage({ route }) {
  const enabled = usePageEnabled('scenarios');
  return (
    <ShellP active="scenarios">
      {enabled === false ? <PluginOff label="场景模拟" what="场景和示例数据" /> : <Scenarios route={route} />}
    </ShellP>
  );
}

function Spark({ values }) {
  const p = sparkPaths(values, 110, 34);
  if (!p) return null;
  return (
    <svg width="110" height="34" style={{ display: 'block' }} aria-hidden="true">
      <path d={p.area} fill="rgba(37,99,235,.10)" />
      <path d={p.line} fill="none" stroke="#2563EB" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

function Scenarios({ route }) {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const [types, setTypes] = useState({});
  const [rows, setRows] = useState(null);
  const [summary, setSummary] = useState(null);
  const [mocks, setMocks] = useState({ items: [], skipped: [] });
  const [mockKey, setMockKey] = useState(null);
  const [preview, setPreview] = useState(null);
  const [tab, setTab] = useState(route?.query?.tab === 'md' ? 'md' : 'sc');
  const [status, setStatus] = useState('all');
  const [slice, setSlice] = useState('');
  const [selected, setSelected] = useState(null);
  const [openKey, setOpenKey] = useState(null);
  const [hit, setHit] = useState(null);
  const located = useRef(null);
  const scrollTo = useRef(null);

  const loadRows = useCallback(() => Promise.all([
    api.artifacts(pid, SCENARIO, { limit: 500 }).then((r) => setRows(r.items || [])),
    api.artifactSummary(pid, SCENARIO, { trend: true }).then(setSummary),
  ]).catch((e) => toast(errorText(e), 'alert')), [api, pid]);
  const loadMocks = useCallback(() => api.artifacts(pid, MOCK).then((r) => {
    setMocks({ items: r.items || [], skipped: r.skipped || [] });
    setMockKey((k) => (k && (r.items || []).some((x) => x.key === k) ? k : r.items?.[0]?.key || null));
  }).catch(() => {}), [api, pid]);

  useEffect(() => {
    api.artifactTypes(pid).then((r) => setTypes(Object.fromEntries((r.items || []).map((t) => [t.type, t])))).catch(() => {});
  }, [api, pid]);
  useEffect(() => { loadRows(); loadMocks(); }, [loadRows, loadMocks]);
  useEffect(() => {
    if (!mockKey) { setPreview(null); return undefined; }
    let alive = true;
    api.artifact(pid, MOCK, mockKey).then((d) => { if (alive) setPreview(d); }).catch(() => {});
    return () => { alive = false; };
  }, [api, pid, mockKey, mocks]);

  const tasks = useCommandTasks('scenarios', { onFinish: () => { loadRows(); loadMocks(); } });
  useProjectEvent('artifacts.updated', (d) => {
    if (d.type === SCENARIO) loadRows();
    if (d.type === MOCK) loadMocks();
  });
  useProjectEvent('scope.updated', () => loadRows());

  // ?key=：定位到那一行（被筛掉了就把筛选放开），滚到中间并高亮
  const routeKey = route?.query?.key || null;
  useEffect(() => {
    if (!routeKey || !rows || located.current === routeKey) return;
    located.current = routeKey;
    const found = rows.find((r) => r.key === routeKey);
    if (!found) { toast('没找到这个场景，可能已经删除', 'alert'); return; }
    setTab('sc');
    setStatus('all');
    setSlice('');
    setSelected(routeKey);
    setHit(routeKey);
    scrollTo.current = routeKey;
  }, [routeKey, rows]);
  // 定位的那一行渲染出来后滚到中间（页签、筛选刚切换时要等这一轮渲染完）
  useEffect(() => {
    const key = scrollTo.current;
    const el = key && document.querySelector(`.sc-tbl tr[data-key="${CSS.escape(key)}"]`);
    if (!el) return;
    scrollTo.current = null;
    el.scrollIntoView({ block: 'center' });
  });

  const sType = types[SCENARIO];
  const all = useMemo(() => rows || [], [rows]);
  const list = useMemo(() => filterRows(all, { status, slice }), [all, status, slice]);
  const counts = countRows(all);
  const rate = passRate(summary, counts);
  const stale = all.filter((r) => rowState(r) === 'stale');
  const lockedRows = all.filter((r) => rowLocked(scope, r, sType));
  const lst = lockState(scope);
  // 本页正在跑命令时进度条已经说明了只读，「助手运行中」的只读条就不重复
  const showLock = lst !== 'none' && lockedRows.length > 0 && !(tasks.active && lst !== 'pending_confirm');
  const target = walkTarget(list.length ? list : all, selected);
  const headWalk = target ? walkCommand(target) : null;
  const batch = batchCommand(all.length);
  const refresh = refreshCommand(stale);
  const skip = skippedNote(mocks.skipped);
  const mockType = types[MOCK];
  const bySlice = showSlice(sType);
  const sliceOptions = [{ value: '', label: '全部分段' }, ...enumOptions(sType, 'slice')];
  const openRow = all.find((r) => r.key === openKey) || null;

  return (
    <>
      <PageHead
        eyebrow={<PluginEyebrow pack="trace" label="场景模拟" />}
        title="场景模拟"
        actions={(
          <>
            <ExtraActions page="scenarios" tasks={tasks} />
            <SendButton tasks={tasks} {...refresh} icon="refresh" kind="soft" disabled={!stale.length}>补齐过期场景 {stale.length}</SendButton>
            <SendButton tasks={tasks} command={headWalk?.command} args={headWalk?.args} text={headWalk?.text} icon="hand" disabled={!headWalk}>交互模拟</SendButton>
            <SendButton tasks={tasks} {...batch} kind="primary" icon="play" disabled={!all.length}>批量验证</SendButton>
          </>
        )}
      />
      {tasks.active || showLock ? (
        <div className="mr-runrow" style={{ margin: '0 0 14px' }}>
          <CommandRunBar task={tasks.active} />
          {showLock ? <ScopeLockBar lock={scope} text={lockBarText(scope, lockedRows)} style={{ margin: 0, flex: 1, alignItems: 'center' }} /> : null}
        </div>
      ) : null}
      <div className="grid4" style={{ gridTemplateColumns: '1.3fr 1fr 1fr 1fr 1fr' }}>
        <div className="stat">
          <div className="lb"><Icon name="gauge" size={14} />推演通过率</div>
          <div className="row" style={{ alignItems: 'flex-end' }}>
            <div className="v">{rate.pct}<small>%</small></div>
            <span className="sp" />
            <Spark values={(summary?.trend || []).map((t) => t.value_pct)} />
          </div>
          <div className="d">{rate.desc}</div>
        </div>
        <Stat label="通过" value={counts.pass} icon="check" />
        <Stat label="失败" value={counts.fail} icon="x" desc={failDesc(all)} />
        <Stat label="过期" value={counts.stale} icon="refresh" desc={staleDesc(all)} />
        <Stat label="示例数据" value={mocks.items.length} unit="组" icon="table" desc={mockDesc(mocks.items)} />
      </div>
      <div className="card mt16">
        <div style={{ padding: '4px 16px 0' }}>
          <Tabs items={[['sc', '场景', all.length, 'flask'], ['md', '示例数据', mocks.items.length, 'table']]} value={tab} onChange={setTab} />
        </div>
        {tab === 'sc' ? (
          <div className="card-b">
            <div className="row mb12">
              <span><Seg items={STATUS_SEG} value={status} onChange={setStatus} /></span>
              {bySlice ? <Select w="150px" icon="layers" options={sliceOptions} value={slice} onChange={setSlice} /> : null}
            </div>
            <div className="tbl-wrap">
              <table className="tbl compact sc-tbl">
                <thead>
                  <tr>
                    <th>场景</th>{bySlice ? <th style={{ width: 150 }}>分段</th> : null}<th style={{ width: 150 }}>来源</th><th style={{ width: 110 }}>输入</th>
                    <th style={{ width: 100 }}>上次推演</th><th style={{ width: 90 }}>状态</th><th style={{ width: 70 }} />
                  </tr>
                </thead>
                <tbody>
                  {list.map((r) => (
                    <Row key={r.key} row={r} type={sType} bySlice={bySlice} lock={scope} tasks={tasks} on={r.key === selected} hit={r.key === hit} onOpen={() => { setSelected(r.key); setOpenKey(r.key); }} />
                  ))}
                </tbody>
              </table>
              {rows && !list.length ? <Empty icon="flask" title={all.length ? '没有符合条件的场景' : '还没有场景'} /> : null}
            </div>
          </div>
        ) : (
          <div className="card-b">
            {skip ? <Note tone="teal" icon="check">已跳过不落库的对象：<b>{skip.labels}</b></Note> : null}
            <div className={cx('grid2', skip && 'mt12')} style={{ gridTemplateColumns: '1fr 1.2fr' }}>
              <div className="tbl-wrap">
                <table className="tbl compact">
                  <thead><tr><th>示例数据</th><th>用于场景</th><th>行数</th><th>生成方式</th></tr></thead>
                  <tbody>
                    {mocks.items.map((m) => (
                      <tr key={m.key} className={cx('sc-mrow', m.key === mockKey && 'on')} onClick={() => setMockKey(m.key)}>
                        <td>{m.fields?.object?.name || m.title}</td>
                        <td>{m.fields?.scenarios}</td>
                        <td>{m.fields?.rows}</td>
                        <td>{enumLabel(mockType, 'mode', m.fields?.mode) || '按场景生成'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div>
                <div className="sec-t">
                  <Icon name="eye" size={12} />预览 · {preview?.title || '—'}
                  <span className="sp" />
                  <SendButton tasks={tasks} {...mockCommand()} size="xs" kind="soft" icon="sparkle">按场景补生成</SendButton>
                </div>
                {preview?.table ? (
                  <div className="tbl-wrap">
                    <table className="tbl compact mono">
                      <thead><tr>{preview.table.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                      <tbody>{preview.table.rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
                    </table>
                  </div>
                ) : null}
              </div>
            </div>
          </div>
        )}
      </div>
      {openRow ? <ScenarioDrawer row={openRow} type={sType} lock={scope} tasks={tasks} onClose={() => setOpenKey(null)} onChanged={loadRows} /> : null}
    </>
  );
}

function Row({ row, type, bySlice, lock, tasks, on, hit, onOpen }) {
  const f = row.fields || {};
  const st = rowState(row);
  const [inputLabel, tone] = INPUT_KIND[f.input_kind] || [f.input_kind, 'line'];
  const locked = rowLocked(lock, row, type);
  const walk = walkCommand(row);
  return (
    <tr data-key={row.key} className={cx('sc-row', on && 'on', hit && 'hit', locked && 'locked')} onClick={onOpen}>
      <td>
        <span className="b">{row.title}</span>
        {locked ? <span className="sc-lk" data-pop={rowLockPop(lock)}><Icon name="lock" size={12} /></span> : null}
      </td>
      {bySlice ? <td><span className="sm">{enumLabel(type, 'slice', f.slice)}</span></td> : null}
      <td>{f.source ? <Ev anchor={f.source} label={f.source_label} /> : <span className="muted sm">—</span>}</td>
      <td><Chip tone={tone} sm>{inputLabel}</Chip></td>
      <td><span className="muted sm num">{lastRun(f.last_run_at)}</span></td>
      <td>{st === 'fail' ? <span data-pop={failHint(row)}><St k="fail" sm /></span> : <St k={st} sm />}</td>
      <td onClick={(e) => e.stopPropagation()}>
        <SendButton tasks={tasks} command={walk.command} args={walk.args} text={walk.text} size="xs" kind="ghost" icon="play" />
      </td>
    </tr>
  );
}
