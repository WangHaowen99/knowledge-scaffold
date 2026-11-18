// 项目设置 · 构建方式（P-13）与自主构建策略（AB-01、AB-02；F8 起不分段，范围是全部没做完的步骤），
// 一个「保存」同时存构建方式和策略；「保存并开始自主构建」只在策略分区底部 = 保存 → AB-03 预览 → 确认 → AB-04 → 去 l4-run（契约 8.1 #41）
import { Fragment, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Icon, KV, Modal, Note, Seg, Toggle, cx,
} from '../../ui/index.jsx';
import {
  previewScope, startErrorText, startLabel, takeOverText,
} from '../workbench/autobuildModel.js';
import {
  advancedSummary, isRecommended, lastRunText, moveItem, optionalStepsText, policyBody, policyDirty, policyForm,
  runStateText, startBlocker, thresholdAt, thresholdPos,
} from './settingsModel.js';

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

// 开始前预览（AB-03 的结果）：样子同工作台里切到自主构建时的预览弹窗
export function StartDialog({ preview: initial, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [preview, setPreview] = useState(initial);
  const [busy, setBusy] = useState(false);
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
          ['范围', previewScope(preview)],
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
  const { pid, meta, setMeta } = useProject();
  const bm = d.buildMode || { build_mode: meta.build_mode };
  const policy = d.policy;
  const [mode, setMode] = useState(bm.build_mode);
  const [form, setForm] = useState(() => policyForm(policy));
  const [adv, setAdv] = useState(false);
  const [busy, setBusy] = useState(null);
  const [preview, setPreview] = useState(null);

  useEffect(() => { setMode(bm.build_mode); }, [bm.build_mode]);
  useEffect(() => { setForm(policyForm(policy)); }, [policy]);

  const modeDirty = mode !== bm.build_mode;
  const pDirty = !!policy && policyDirty(policy, form);
  const locked = archived;
  const modeLocked = archived || !!active;
  const blocker = startBlocker({ archived, active });

  const setF = (k, fn) => setForm((f) => ({ ...f, [k]: fn(f[k]) }));
  const setScope = (patch) => setF('scope', (s) => ({ ...s, ...patch }));
  const setAdvanced = (patch) => setF('advanced', (a) => ({ ...a, ...patch }));

  const saveMode = async () => {
    const r = await api.setBuildMode(pid, mode);
    put('buildMode', r);
    setMeta({ ...meta, build_mode: r.build_mode });
    return r;
  };
  const savePolicy = async () => {
    try {
      const r = await api.saveAutobuildPolicy(pid, policyBody(form, policy.revision));
      put('policy', r);
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
      toast(errorText(e), 'alert');
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
      toast(errorText(e), 'alert');
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
  const optionalText = optionalStepsText(policy);
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
            {/* F8 起不分段：范围就是全部没做完的步骤，这里只选带不带过期、可选步骤（老策略里选的分段由服务端按全部处理）；可选步骤按 AB-01 optional_nodes 列名字，没有时不显示开关 */}
            <Card title="范围" icon="layers">
              <div className="sm">全部没做完的步骤</div>
              <div className="row wrap mt12" style={{ gap: 18 }}>
                <span className="row sm"><Toggle on={scope.refresh_stale_first} disabled={locked} onChange={(v) => setScope({ refresh_stale_first: v })} />先处理过期的内容</span>
                {optionalText ? <span className="row sm"><Toggle on={scope.include_optional} disabled={locked} onChange={(v) => setScope({ include_optional: v })} />{optionalText}</span> : null}
              </div>
            </Card>

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
      {preview ? <StartDialog preview={preview} onClose={closePreview} /> : null}
    </div>
  );
}
