// 项目设置 · 构建方式（P-13）与自主构建策略（AB-01、AB-02，范围取 BP-01 + FL-01，生产库授权行取 EV-01），
// 一个「保存」同时存构建方式和策略；「保存并开始自主构建」只在策略分区底部 = 保存 → AB-03 预览 → 确认 → AB-04 → 去 l4-run（契约 8.1 #41）
import { Fragment, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Check, Chip, Field, Icon, Inp, KV, Modal, Note, Radio, Seg, St, Toggle, cx,
} from '../../ui/index.jsx';
import { evidenceFromSources } from '../../state/evidence.js';
import { startErrorText, startLabel, takeOverText } from '../workbench/autobuildModel.js';
import {
  advancedSummary, finalizePolicy, isRecommended, lastRunText, moveItem, pendingSliceIds, policyBody, policyDirty, policyForm,
  runStateText, scopeText, sliceRows, startBlocker, thresholdAt, thresholdPos, windowText,
} from './settingsModel.js';

const ENV_LABEL = { production: '生产环境', staging: '预发环境', test: '测试环境', dev: '开发环境' };

function ThresholdSlider({ value, onChange, disabled }) {
  const ref = useRef(null);
  const at = (e) => {
    const r = ref.current.getBoundingClientRect();
    onChange(thresholdAt(((e.clientX - r.left) / r.width) * 100));
  };
  const down = (e) => {
    if (disabled) return;
    ref.current.setPointerCapture?.(e.pointerId);
    at(e);
  };
  const move = (e) => { if (!disabled && e.buttons) at(e); };
  const key = (e) => {
    if (disabled) return;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); onChange(Math.max(0.5, Math.round((value - 0.05) * 20) / 20)); }
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); onChange(Math.min(1, Math.round((value + 0.05) * 20) / 20)); }
  };
  const pos = thresholdPos(value);
  return (
    <div
      className="g2-slider" ref={ref} tabIndex={0} role="slider" aria-valuemin={0.5} aria-valuemax={1} aria-valuenow={value}
      onPointerDown={down} onPointerMove={move} onKeyDown={key}
    >
      <div className="tr" /><div className="fi" style={{ width: `${pos}%` }} /><span className="kn" style={{ left: `${pos}%` }} />
      {[0.5, 0.6, 0.7, 0.8, 0.9, 1].map((t) => <span key={t} className="tk" style={{ left: `${thresholdPos(t)}%` }}>{t.toFixed(1)}</span>)}
    </div>
  );
}

function Sec({ no, title, desc, children }) {
  return (
    <div className="g2-sec">
      <h4><span className="no">{no}</span>{title}</h4>
      {desc ? <div className="d">{desc}</div> : null}
      <div className="bd">{children}</div>
    </div>
  );
}

// 冲突时的取值顺序：拖动调整
function PriorityChain({ items, onChange, disabled }) {
  const [drag, setDrag] = useState(null);
  const [over, setOver] = useState(null);
  return (
    <div className="g2-chain">
      {items.map((t, i) => (
        <Fragment key={t}>
          {i ? <span className="faint"><Icon name="chev" size={12} /></span> : null}
          <span
            className={cx('chip line', drag === i && 'drag', over === i && drag !== i && 'over')}
            draggable={!disabled}
            onDragStart={(e) => { setDrag(i); e.dataTransfer.effectAllowed = 'move'; }}
            onDragOver={(e) => { e.preventDefault(); setOver(i); }}
            onDragLeave={() => setOver((o) => (o === i ? null : o))}
            onDrop={(e) => { e.preventDefault(); if (drag != null) onChange(moveItem(items, drag, i)); setDrag(null); setOver(null); }}
            onDragEnd={() => { setDrag(null); setOver(null); }}
          >{t}</span>
        </Fragment>
      ))}
      <span className="xs muted">拖动调整顺序</span>
    </div>
  );
}

function sliceChip(r) {
  if (r.group === 'done') return <St k="confirmed" sm />;
  if (r.group === 'wait') return <Chip tone="amber" sm>{r.note || '待确认'}</Chip>;
  if (r.group === 'now') return <St k="pending" sm label={r.note || '进行中'} />;
  return <St k="ready" sm />;
}

// 开始前预览（AB-03 的结果）：样子同建模蓝图「一路建完」的预览弹窗
export function StartDialog({ preview: initial, evidenceOn, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [preview, setPreview] = useState(initial);
  const [busy, setBusy] = useState(false);
  const all = preview.slices || [];
  const included = all.filter((s) => s.included);
  const start = async () => {
    setBusy(true);
    try {
      const run = await api.startAutobuild(pid, { preview_token: preview.preview_token, ...(preview.busy ? { take_over: true } : {}) });
      toast(run.state === 'queued' ? `运行 #${run.run_no} 已排队，轮到时自动开始` : '自主构建已开始', 'rocket');
      onClose(true);
      navigate(pagePath('l4-run', pid), run.conversation_id ? { c: run.conversation_id } : undefined);
    } catch (e) {
      if (e.code === 'preview_expired') {
        try { setPreview(await api.previewAutobuild(pid, {})); toast('预览已刷新，请再确认', 'refresh'); } catch (e2) { toast(errorText(e2), 'alert'); }
      } else toast(startErrorText(e, errorText), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open onClose={() => onClose(false)} icon="rocket" title="开始自主构建" w={580}
      foot={(
        <>
          <Button onClick={() => onClose(false)}>取消</Button>
          <Button kind="l4" icon="rocket" disabled={busy} onClick={start}>{startLabel(preview)}</Button>
        </>
      )}
    >
      <KV
        w="72px"
        pairs={[
          ['范围', (
            <div>
              <div>{included.length} 段{included.length < all.length ? `（另有 ${all.length - included.length} 段不在范围）` : ''}</div>
              <div className="row wrap mt4" style={{ gap: 6 }}>
                {all.map((s) => <Chip key={s.id} tone={s.included ? 'line' : 'gray'} sm>{s.name}{s.current ? ` · ${s.current}` : ''}</Chip>)}
              </div>
            </div>
          )],
          ...(evidenceOn ? [['生产库', preview.production || '—']] : []),
          ['拿不准时', preview.below_threshold_text || '—'],
        ]}
      />
      {(preview.warnings || []).map((w) => <Note key={w} tone="amber" className="mt12">{w}</Note>)}
      {preview.busy ? <Note tone="amber" className="mt12">{takeOverText(preview.busy)}</Note> : null}
      {preview.queued ? <Note tone="gray" className="mt12">平台已满，开始后排队{preview.queue_position ? `（第 ${preview.queue_position} 位）` : ''}</Note> : null}
    </Modal>
  );
}

export function BuildSection({ d, put, reload, archived, active, autoStart, onAutoStarted }) {
  const { api } = useApp();
  const { pid, meta, setMeta, flow, hasPlugin } = useProject();
  const bm = d.buildMode || { build_mode: meta.build_mode };
  const policy = d.policy;
  const [mode, setMode] = useState(bm.build_mode);
  const [form, setForm] = useState(() => policyForm(policy));
  const [drafts, setDrafts] = useState({});
  const [adv, setAdv] = useState(false);
  const [busy, setBusy] = useState(null);
  const [errors, setErrors] = useState({});
  const [preview, setPreview] = useState(null);

  useEffect(() => { setMode(bm.build_mode); }, [bm.build_mode]);
  useEffect(() => { setForm(policyForm(policy)); setDrafts({}); setErrors({}); }, [policy]);

  const rows = sliceRows(d.blueprint?.slices, flow?.slices);
  const pendingIds = pendingSliceIds(rows);
  const final = finalizePolicy(form, drafts);
  const modeDirty = mode !== bm.build_mode;
  const pDirty = !!policy && policyDirty(policy, final.form);
  const locked = archived;
  const modeLocked = archived || !!active;
  const blocker = startBlocker({ archived, active, rows });
  const limits = policy?.platform_limits || {};
  const sources = d.sources?.items || [];
  // 证据源本期只留接口（EV-01 回 501）：读不到数据源登记时不出生产库采集授权（策略里的字段保留，ADR-0056）
  const evidenceOn = evidenceFromSources(d.sources);

  const setF = (k, fn) => setForm((f) => ({ ...f, [k]: fn(f[k]) }));
  const setScope = (patch) => setF('scope', (s) => ({ ...s, ...patch }));
  const setAdvanced = (patch) => setF('advanced', (a) => ({ ...a, ...patch }));
  const setProd = (sid, patch) => setF('production_collection', (list) => list.map((x) => (x.source_id === sid ? { ...x, ...patch } : x)));
  const setDraft = (sid, patch) => setDrafts((x) => ({ ...x, [sid]: { ...x[sid], ...patch } }));

  const saveMode = async () => {
    const r = await api.setBuildMode(pid, mode);
    put('buildMode', r);
    setMeta({ ...meta, build_mode: r.build_mode });
    return r;
  };
  const savePolicy = async () => {
    if (Object.keys(final.problems).length) {
      setErrors(final.problems);
      throw Object.assign(new Error(Object.values(final.problems)[0]), { local: true });
    }
    try {
      const r = await api.saveAutobuildPolicy(pid, policyBody(final.form, policy.revision));
      put('policy', r);
      setErrors({});
      return r;
    } catch (e) {
      if (e.code === 'revision_conflict') reload('policy');
      throw e;
    }
  };

  // 一个「保存」：构建方式和策略有改动的都存
  const onSave = async () => {
    setBusy('save');
    try {
      if (modeDirty) await saveMode();
      if (pDirty) await savePolicy();
      toast('已保存');
    } catch (e) {
      toast(e.local ? e.message : errorText(e), 'alert');
    } finally {
      setBusy(null);
    }
  };
  // 保存（有改动的话）→ AB-03 预览 → 弹窗确认
  const start = async ({ save = true } = {}) => {
    if (blocker) { toast(blocker, 'alert'); return; }
    setBusy('start');
    try {
      if (save && modeDirty) await saveMode();
      if (save && pDirty) await savePolicy();
      setPreview(await api.previewAutobuild(pid, {}));
    } catch (e) {
      toast(e.local ? e.message : errorText(e), 'alert');
    } finally {
      setBusy(null);
    }
  };
  // ⌘K「开始自主构建」：不改策略，直接按已保存的策略预览
  const startRef = useRef(start);
  startRef.current = start;
  useEffect(() => {
    if (!autoStart) return;
    onAutoStarted();
    startRef.current({ save: false });
  }, [autoStart, onAutoStarted]);

  const closePreview = (started) => {
    setPreview(null);
    if (started) reload('runs', 'buildMode', 'policy');
  };

  const scope = form.scope || {};
  const all = scope.mode !== 'selected';
  const picked = (id) => (all ? true : (scope.slice_ids || []).includes(id));
  const toggleSlice = (id) => setScope({ slice_ids: picked(id) ? scope.slice_ids.filter((x) => x !== id) : [...(scope.slice_ids || []), id] });
  const a = form.advanced || {};
  const last = policy?.last_run;

  return (
    <div>
      <div className="card" data-sec="build-mode">
        <div className="card-h">
          <h3>构建方式</h3><span className="sp" />
          <Button size="sm" kind="primary" disabled={!(modeDirty || pDirty) || locked || !!busy} onClick={onSave}>保存</Button>
        </div>
        <div className="card-b">
          <div className="grid2">
            {[
              ['L2', 'users', '陪伴建模', '每个决定等你确认'],
              ['L4', 'rocket', '自主构建', '按范围一路建完，结束后集中复核'],
            ].map(([k, icon, title, desc]) => (
              <div key={k} className={cx('evd-eng evd-bm', mode === k && 'on', modeLocked && mode !== k && 'lock')} onClick={() => { if (!modeLocked) setMode(k); else if (active) toast('自主构建运行中，构建方式等这次运行结束后再改', 'lock'); }}>
                <span className="ic"><Icon name={icon} size={18} /></span>
                <div className="grow"><div className="row"><b>{title}</b></div><div className="sm muted mt4">{desc}</div></div>
                <span className={cx('rad', mode === k && 'on')} />
              </div>
            ))}
          </div>
          {active ? (
            <div className="mt12">
              <Note tone="amber" icon="lock">
                {runStateText(active) || '自主构建运行中'}，结束后才能改构建方式。
                <a className="link" style={{ marginLeft: 6 }} onClick={() => navigate(pagePath('l4-run', pid))}>查看运行</a>
              </Note>
            </div>
          ) : null}
        </div>
      </div>

      <div className="evd-l4w" data-sec="autobuild">
        <div className="ps-sech"><Icon name="sliders" size={15} /><h3>自主构建策略</h3></div>
        {!policy ? <Note tone="red">自主构建策略没有读出来，刷新页面再试。</Note> : (
          <div className="g2s">
            <Card title="范围" icon="layers">
              <div className="row g16 mb12">
                <span className="g2s-r"><Radio on={all} label={`全部未完成的分段（${pendingIds.length} 段）`} onChange={() => !locked && setScope({ mode: 'all_pending', slice_ids: pendingIds })} /></span>
                <span className="g2s-r"><Radio on={!all} label="只跑选中的分段" onChange={() => !locked && setScope({ mode: 'selected', slice_ids: all ? pendingIds : scope.slice_ids })} /></span>
              </div>
              {rows.length ? (
                <div className={cx('g2s-sl', (all || locked) && 'g2-off')}>
                  {rows.map((r) => (
                    <div key={r.id} className="row" style={{ padding: '7px 10px', border: '1px solid var(--line)', borderRadius: 10, ...(r.done ? { opacity: 0.55 } : null) }}>
                      <Check on={!r.done && picked(r.id)} disabled={r.done} onChange={() => toggleSlice(r.id)} />
                      <span className="grow">{r.name}</span>
                      {sliceChip(r)}
                    </div>
                  ))}
                </div>
              ) : <div className="sm muted">分段还没规划</div>}
              {errors.scope ? <div className="xs mt8" style={{ color: 'var(--red)' }}>{errors.scope}</div> : null}
              <div className="row wrap mt12" style={{ gap: 18 }}>
                <span className="row sm"><Toggle on={scope.refresh_stale_first} disabled={locked} onChange={(v) => setScope({ refresh_stale_first: v })} />先处理过期的内容</span>
                {hasPlugin('scenarios') ? <span className="row sm"><Toggle on={scope.generate_scenarios} disabled={locked} onChange={(v) => setScope({ generate_scenarios: v })} />生成场景并推演</span> : null}
                <span className="row sm"><Toggle on={scope.include_optional} disabled={locked} onChange={(v) => setScope({ include_optional: v })} />包括可选步骤：规则与指标、流程图</span>
              </div>
            </Card>

            {evidenceOn ? <Card title="生产库采集授权" icon="db" sub="没授权的数据源，需要采集时会挂起该项，不会自己去连">
              {(form.production_collection || []).length ? (form.production_collection || []).map((row) => {
                const src = sources.find((s) => s.id === row.source_id);
                const dr = drafts[row.source_id] || {};
                const werr = errors[`window:${row.source_id}`];
                const allowedByPlatform = limits.allow_production_collection !== false;
                return (
                  <div key={row.source_id} className={cx('g2-guard g2s-prod', !row.allowed && 'g2-off')} style={{ background: '#fff', flexWrap: 'wrap' }}>
                    <Icon name="db" size={14} />
                    <b style={{ fontWeight: 600 }}>{src?.display_name || row.source_id}</b>
                    <span className="xs muted">业务系统 · {ENV_LABEL[src?.environment] || '生产环境'}</span>
                    <span className="sp" />
                    <span className="row sm" title={allowedByPlatform ? undefined : '平台不允许自主构建采集生产库'}>
                      <Toggle on={row.allowed} disabled={locked || !allowedByPlatform} onChange={(v) => setProd(row.source_id, { allowed: v })} />允许自主构建时采集
                    </span>
                    <div className="g2s-mf">
                      <Field label="时间窗口" hint={werr ? <span style={{ color: 'var(--red)' }}>{werr}</span> : null}>
                        <Inp icon="clock" placeholder="22:00 – 06:00" error={!!werr} disabled={locked} value={dr.window ?? windowText(row.window)} onChange={(e) => setDraft(row.source_id, { window: e.target.value })} />
                      </Field>
                      <Field label="采集范围">
                        <Inp icon="filter" placeholder="补货标准表、商品品类表" disabled={locked} value={dr.scope ?? scopeText(row.scope)} onChange={(e) => setDraft(row.source_id, { scope: e.target.value })} />
                      </Field>
                    </div>
                  </div>
                );
              }) : <div className="sm muted">本项目还没有授权的生产库数据源；在「资料 › 业务系统数据源」里登记并授权后会出现在这里。</div>}
            </Card> : null}

            <div className="card">
              <div className="g2-advh" onClick={() => setAdv((v) => !v)}>
                <Icon name="sliders" size={14} /><b>高级设置</b>
                <span className="chip gray sm">{isRecommended(a) ? '已按推荐值设置' : '已调整'}</span>
                <span className="xs muted grow ell">{advancedSummary(a)}</span>
                <span><Icon name={adv ? 'chevu' : 'chevd'} size={14} /></span>
              </div>
              {adv ? (
                <div style={{ borderTop: '1px solid var(--line)' }}>
                  <Sec no="A" title="置信度阈值">
                    <div className="row"><span className="sp" /><span className="chip blue num">≥ {Number(a.confidence_threshold).toFixed(2)} 直接采用推荐</span></div>
                    <ThresholdSlider value={a.confidence_threshold} disabled={locked} onChange={(v) => setAdvanced({ confidence_threshold: v })} />
                  </Sec>
                  <Sec no="B" title="低于阈值时">
                    <div className="grid2" style={{ gap: 8 }}>
                      {[
                        ['conservative_assumption', 'A', '先按最稳妥的做法往下做，做完列出来给你复核'],
                        ['suspend', 'B', '先跳过这一项，留给你来定'],
                      ].map(([k, key, t]) => (
                        <div key={k} className={cx('opt', a.below_threshold === k && 'on')} onClick={() => !locked && setAdvanced({ below_threshold: k })}>
                          <span className="k">{key}</span><div><div className="t">{t}</div></div>
                        </div>
                      ))}
                    </div>
                  </Sec>
                  <Sec no="C" title="资料优先级" desc="冲突时按这个顺序取值">
                    <PriorityChain items={a.source_priority || []} disabled={locked} onChange={(v) => setAdvanced({ source_priority: v })} />
                  </Sec>
                  <Sec no="D" title="修复轮次上限">
                    <div className="row">
                      <Seg items={[['1', '1 轮'], ['2', '2 轮'], ['3', '3 轮']]} value={String(a.max_fix_rounds)} onChange={(v) => !locked && setAdvanced({ max_fix_rounds: Number(v) })} />
                    </div>
                  </Sec>
                </div>
              ) : null}
            </div>

            <div className="g2s-foot">
              <span className="xs muted">{active ? runStateText(active) : lastRunText(last)}</span>
              <span className="sp" />
              <Button size="sm" kind="l4" icon="rocket" disabled={!!busy || !!blocker} title={blocker || undefined} onClick={() => start()}>保存并开始自主构建</Button>
            </div>
          </div>
        )}
      </div>
      {preview ? <StartDialog preview={preview} evidenceOn={evidenceOn} onClose={closePreview} /> : null}
    </div>
  );
}
