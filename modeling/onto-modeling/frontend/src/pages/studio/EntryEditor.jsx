// 条目编辑器（设计稿 studio-table 右栏）：结构化编辑、草稿自动暂存（ST-07）、提交三路结果（ST-08）、
// 放弃草稿、写入范围锁定条；「编辑原文（高级）」切到 RawEditor（F-02 / F-03）
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText, relativeTime } from '../../api/errors.js';
import { setWorkspaceDirty } from '../../lib/workspaceDirty.js';
import {
  Area, Button, Chip, Field, Icon, Inp, Modal, Note, cx, useOutside,
} from '../../ui/index.jsx';
import { reportRefs, researchReportId } from '../reports/reportModel.js';
import EvidencePicker from './EvidencePicker.jsx';
import { KindChip, KindDot, LockBar, askAssistant, useLockCopy } from './parts.jsx';
import {
  CONFIDENCE_CHIP, anchorChipLabel, commitSummary, fieldErrorMap, formFromEntry, hhmm, kindLabel,
  rejectSummary, resolutionLabel, sectionAnchors, summarizeFormChanges,
} from './studioModel.js';

const AUTOSAVE_MS = 2000;
// 编辑原文带 CodeMirror，用到时再加载
const RawEditor = lazy(() => import('./RawEditor.jsx'));

function EvidenceInput({ field, cell, label: resolved, onChange, onPick, err }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  if (field.expert_confirmed) {
    return (
      <div className="stt-evc" onClick={() => onChange({ expert_confirmed: false })} title="点一下取消专家确认">
        <Icon name="user" size={12} /><span className="ell grow">专家确认</span>
      </div>
    );
  }
  if (field.evidence_anchor) {
    const same = cell?.anchor === field.evidence_anchor;
    const label = same ? cell.label : resolved || anchorChipLabel(field.evidence_anchor);
    const scheme = same ? cell.scheme : field.evidence_anchor.split(':')[0];
    const [conf, tone] = (same && CONFIDENCE_CHIP[cell.confidence]) || [];
    return (
      <div className="stt-evc" data-ev={field.evidence_anchor}>
        <Icon name={scheme === 'db' ? 'db' : 'quote'} size={12} />
        <span className="ell grow">{label}</span>
        {conf ? <Chip tone={tone} sm>{conf}</Chip> : null}
      </div>
    );
  }
  return (
    <>
      <div ref={ref} className={cx('cell add', err && 'err')} onClick={() => setOpen((v) => !v)}>+ 补出处或专家确认</div>
      {open ? (
        <EvidencePicker
          anchorRef={ref}
          onClose={() => setOpen(false)}
          onPick={(anchor, label) => { setOpen(false); onPick(anchor, label); }}
          onExpert={() => { setOpen(false); onChange({ expert_confirmed: true }); }}
        />
      ) : null}
    </>
  );
}

function RelationPicker({ rows, exclude, onPick }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const hits = rows.filter((r) => r.kind !== 'flowchart' && !exclude.includes(r.id) && (!q || r.name.includes(q) || r.key.includes(q))).slice(0, 12);
  return (
    <span className="stt-rel" ref={ref}>
      <span className="chip gray" style={{ cursor: 'pointer' }} onClick={() => setOpen((v) => !v)}><Icon name="plus" />添加关联</span>
      {open ? (
        <div className="menu">
          <Inp icon="search" autoFocus placeholder="搜索条目名、Key" value={q} onChange={(e) => setQ(e.target.value)} />
          {hits.map((r) => (
            <div key={r.id} className="mi" onClick={() => { onPick(r); setOpen(false); setQ(''); }}>
              <KindDot kind={r.kind} size={8} /><span>{r.name}</span><span className="k">{kindLabel(r.kind)}</span>
            </div>
          ))}
          {!hits.length ? <div className="xs muted" style={{ padding: '6px 10px' }}>没有匹配的条目</div> : null}
        </div>
      ) : null}
    </span>
  );
}

// 数据来源：表名、数据库锚点，或「无」
function dsText(form, entry) {
  const ds = form.data_source || {};
  if (ds.kind === 'none') return '无';
  if (entry?.data_source?.value && entry.data_source.value === ds.value) return entry.data_source.label;
  return ds.value || '';
}
function parseDs(text) {
  const t = text.trim();
  if (!t || t === '无') return { kind: 'none' };
  if (/^db:/.test(t)) return { kind: 'anchor', value: t };
  return { kind: 'table', value: t };
}

function ResultNote({ result, pid }) {
  if (!result) return null;
  if (result.kind === 'pass') {
    return <Note tone="teal" icon="check"><b>检查通过，已直接保存为新版本 {result.version}</b>{result.summary ? `　${result.summary}` : ''}</Note>;
  }
  if (result.kind === 'fail') {
    return <Note tone="red" icon="alert"><b>没通过检查：</b>{result.summary || ''}</Note>;
  }
  return (
    <div className="note">
      <Icon name="sparkle" />
      <div className="grow">
        <b>已交给助手复核：</b>
        <ul style={{ paddingLeft: 18, marginTop: 4 }}>{result.points.map((p) => <li key={p}>{p}</li>)}</ul>
        <div className="mt8">
          <Button size="xs" icon="msg" onClick={() => navigate(pagePath('wb-l2', pid), result.cid ? { c: result.cid } : undefined)}>去对话里看</Button>
        </div>
      </div>
    </div>
  );
}

export default function EntryEditor({ id, rows, onChanged, tick }) {
  const { api } = useApp();
  const { pid, scope, meta } = useProject();
  const lockFor = useLockCopy();
  const [entry, setEntry] = useState(null);
  const [error, setError] = useState(null);
  const [form, setForm] = useState(null);
  const [hasDraft, setHasDraft] = useState(false);
  const [loadedDraftAt, setLoadedDraftAt] = useState(null);
  const [save, setSave] = useState({ kind: 'idle', at: null });
  const [result, setResult] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const [dsDraft, setDsDraft] = useState(null);
  const [anchorLabels, setAnchorLabels] = useState({});
  const dirty = useRef(false);
  const formRef = useRef(null);
  formRef.current = form;
  const [, setNow] = useState(0);

  const load = useCallback(async ({ keepResult } = {}) => {
    try {
      const e = await api.studioEntry(pid, id);
      setEntry(e);
      setError(null);
      const d = e.draft;
      setForm(d?.form || formFromEntry(e));
      setHasDraft(!!d);
      setLoadedDraftAt(d ? d.updated_at : null);
      setSave({ kind: 'idle', at: d?.updated_at || null });
      setDsDraft(null);
      dirty.current = false;
      if (!keepResult) setResult(null);
    } catch (e) {
      setError(e);
    }
  }, [api, pid, id]);

  useEffect(() => { setEntry(null); setForm(null); setRaw(false); setResult(null); load(); }, [load]);
  // 「草稿已暂存 · N 分钟前」每半分钟走一次
  useEffect(() => { const t = setInterval(() => setNow((n) => n + 1), 30000); return () => clearInterval(t); }, []);

  const baseRevision = entry?.draft?.base_revision || entry?.revision;
  // 返回 {ok, saved}：saved 表示这次确实存了一份草稿
  const putDraft = useCallback(async () => {
    if (!dirty.current || !formRef.current) return { ok: true, saved: false };
    dirty.current = false;
    setSave({ kind: 'saving', at: null });
    try {
      const r = await api.saveStudioDraft(pid, id, { base_revision: baseRevision, form: formRef.current });
      setHasDraft(true);
      setEntry((e) => (e ? { ...e, draft: r.draft, status: 'editing' } : e));
      setSave({ kind: 'saved', at: r.saved_at });
      return { ok: true, saved: true };
    } catch (e) {
      dirty.current = true;
      setSave({ kind: 'error', at: null });
      // 表单写不进原文（改 Key、这类条目的数据来源、加不了的关联）：服务端的说法更具体
      const unsupported = Object.values(e.details?.fields || {}).some((c) => c === 'rename_unsupported' || c === 'unsupported');
      toast(unsupported && e.serverMessage ? e.serverMessage : errorText(e), 'alert');
      return { ok: false, saved: false };
    }
  }, [api, pid, id, baseRevision]);

  // 自动暂存：停手 2 秒存一次（契约 ST-07 建议节流）
  useEffect(() => {
    if (!dirty.current) return undefined;
    const t = setTimeout(putDraft, AUTOSAVE_MS);
    return () => clearTimeout(t);
  }, [form, putDraft]);
  // 有草稿或没存完的修改时挡住版本回退（develop 的 studio-editor 同口径）
  useEffect(() => {
    setWorkspaceDirty('studio-editor', hasDraft || dirty.current || save.kind === 'saving' || save.kind === 'error');
    return () => setWorkspaceDirty('studio-editor', false);
  }, [hasDraft, save.kind, form]);
  // 换条目、离开页面前把没存的存掉
  const flushRef = useRef(putDraft);
  flushRef.current = putDraft;
  useEffect(() => () => { flushRef.current(); }, [id]);

  // 章节里写的资料出处（规则「原始资料来源」等）：在章节下面列成芯片，点开去资料区定位
  const sectionRefs = useMemo(
    () => Object.fromEntries(Object.entries(form?.sections || {}).map(([title, text]) => [title, sectionAnchors(text)])),
    [form?.sections],
  );
  // 草稿里新填的证据锚点、章节里的出处：经 EV-29 解析出业务说法
  const unknownAnchors = [...(form?.fields || []).map((f) => f.evidence_anchor), ...Object.values(sectionRefs).flat()]
    .filter((a, i, all) => a && all.indexOf(a) === i && !(a in anchorLabels) && !(entry?.fields || []).some((f) => f.evidence?.anchor === a));
  const unknownKey = unknownAnchors.join('|');
  useEffect(() => {
    if (!unknownKey) return;
    api.resolveAnchors(pid, unknownKey.split('|'))
      .then((r) => setAnchorLabels((m) => ({ ...m, ...Object.fromEntries((r.items || []).map((x) => [x.anchor, resolutionLabel(x) || ''])) })))
      .catch(() => {});
  }, [api, pid, unknownKey]);

  const change = (patch) => {
    dirty.current = true;
    setForm((f) => ({ ...f, ...patch }));
  };
  const setField = (i, patch) => change({ fields: form.fields.map((f, j) => (j === i ? { ...f, ...patch } : f)) });

  const ref = entry ? { kind: entry.kind, id: entry.id, key: entry.key, name: entry.name } : null;
  const lockInfo = entry ? lockFor(ref, entry.name) : null;
  const errs = useMemo(() => (result?.kind === 'fail' ? fieldErrorMap(result.raw) : {}), [result]);
  const changes = useMemo(() => (entry && form ? summarizeFormChanges(entry, form) : []), [entry, form]);

  const submit = async () => {
    const flushed = await putDraft();
    if (!flushed.ok) return;
    if (!hasDraft && !flushed.saved) { toast('没有改动', 'info'); return; }
    const before = entry;
    const submitted = formRef.current;
    setSubmitting(true);
    try {
      const r = await api.submitStudioEntry(pid, id, {});
      if (r.result === 'committed') {
        const label = (anchor) => r.entry?.fields?.find((f) => f.evidence?.anchor === anchor)?.evidence?.label;
        setResult({ kind: 'pass', version: r.version_label, summary: commitSummary(summarizeFormChanges(before, submitted), submitted, label) });
        toast(`检查通过，已保存为新版本 ${r.version_label}`, 'check');
        await load({ keepResult: true });
      } else if (r.result === 'rejected') {
        setResult({ kind: 'fail', summary: rejectSummary(r), raw: r });
      } else {
        setResult({ kind: 'review', points: r.review_points || [], cid: r.conversation_id });
        await load({ keepResult: true });
      }
      onChanged?.();
    } catch (e) {
      toast(errorText(e), e.isLocked ? 'lock' : 'alert');
    } finally {
      setSubmitting(false);
    }
  };

  const discard = async () => {
    setDiscardOpen(false);
    try {
      dirty.current = false;
      if (hasDraft) await api.discardStudioDraft(pid, id);
      toast('已放弃草稿', 'undo');
      await load();
      onChanged?.();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  // 别处改了这个条目（助手、MCP）：没有本地改动时重新读
  useEffect(() => { if (tick && !dirty.current && !raw) load({ keepResult: true }); }, [tick]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) {
    return <div className="stt-r card"><div className="sd-empty">{errorText(error)}</div></div>;
  }
  if (!entry || !form) {
    return <div className="stt-r card"><div className="sd-empty">正在读取…</div></div>;
  }

  const isObject = entry.kind === 'object';
  const fieldsByCol = new Map((entry.fields || []).map((f) => [f.column, f.evidence]));
  const exclude = [entry.id, ...(form.relations || [])];
  const relRows = (form.relations || []).map((rid) => rows.find((r) => r.id === rid) || { id: rid, name: rid.split(':').pop() });
  const saveText = save.kind === 'saving' ? '正在暂存…'
    : save.kind === 'error' ? '暂存失败，稍后再试'
      : hasDraft ? `草稿已暂存 · ${relativeTime(save.at || entry.draft?.updated_at) || '刚刚'}` : '';

  return (
    <div className="stt-r card">
      <div className="card-h" style={{ paddingBottom: 12 }}>
        <KindDot kind={entry.kind} />
        <h3 style={{ fontSize: 16 }}>{entry.name}</h3>
        <KindChip kind={entry.kind} />
        <span className="mono xs muted">{entry.key}</span>
        <span className="sp" />
        <Button size="sm" icon="sparkle" onClick={() => askAssistant(pid, entry.id)}>问助手</Button>
        <Button size="sm" kind="ghost" icon={raw ? 'arrowl' : 'code'} onClick={async () => { if (!raw) await putDraft(); setRaw((v) => !v); if (raw) load({ keepResult: true }); }}>
          {raw ? '返回结构化编辑' : '编辑原文（高级）'}
        </Button>
      </div>
      {raw ? (
        <Suspense fallback={<div className="stt-raw"><div className="sd-empty">正在打开原文…</div></div>}>
          <RawEditor
            entry={entry}
            lockInfo={lockInfo}
            lock={scope}
            onBack={() => { setRaw(false); load(); }}
            onSaved={() => onChanged?.()}
          />
        </Suspense>
      ) : (
        <>
          <div className="stt-bar">
            <span className="row g4"><Icon name="history" size={12} />{loadedDraftAt ? `已载入上次未保存的草稿（${hhmm(loadedDraftAt)}）` : `当前版本 ${meta?.version_label || ''}`}</span>
            {saveText ? (
              <>
                <span>·</span>
                <span className="row g4">{save.kind === 'error' ? <Icon name="alert" size={12} /> : save.kind === 'saving' ? <span className="spin" /> : <Icon name="check" size={12} />}{saveText}</span>
              </>
            ) : null}
          </div>
          {lockInfo || result ? (
            <div style={{ padding: '12px 18px 0' }}>
              {lockInfo ? <LockBar info={lockInfo} lock={scope} /> : <ResultNote result={result} pid={pid} />}
            </div>
          ) : null}
          <div className={cx('bd', lockInfo && 'stt-lock')}>
            {entry.draft && !entry.draft.form ? <Note tone="gray" icon="code" className="mb12">这份草稿在原文里，去「编辑原文（高级）」继续</Note> : null}
            <div className="grid2">
              <Field label="名称" req><Inp value={form.name} onChange={(e) => change({ name: e.target.value })} /></Field>
              <Field label="Key"><Inp mono value={form.key} onChange={(e) => change({ key: e.target.value })} /></Field>
            </div>
            <div className="mt12">
              <Field label="描述"><Area minHeight={58} textareaStyle={{ height: 40 }} value={form.description} onChange={(e) => change({ description: e.target.value })} /></Field>
            </div>
            {isObject ? (
              <>
                <div className="sec-t mt16">属性 · {form.fields.length}<span className="sp" /><Button size="xs" kind="ghost" icon="plus" onClick={() => change({ fields: [...form.fields, { name: '', column: '', type: '文本', evidence_anchor: null, expert_confirmed: false }] })}>添加属性</Button></div>
                <div className="tbl-wrap">
                  <table className="tbl stt-ed" style={{ tableLayout: 'fixed' }}>
                    <colgroup><col style={{ width: 112 }} /><col style={{ width: 150 }} /><col style={{ width: 92 }} /><col /></colgroup>
                    <thead><tr><th>属性</th><th>列名</th><th>类型</th><th>证据</th></tr></thead>
                    <tbody>
                      {form.fields.map((f, i) => {
                        const colErr = errs[`fields[${i}].column`];
                        const evErr = errs[`fields[${i}].evidence`];
                        return (
                          <tr key={i}>
                            <td><input className="cell" value={f.name} placeholder="属性" onChange={(e) => setField(i, { name: e.target.value })} /></td>
                            <td>
                              <input className={cx('cell mono', colErr && 'err')} value={f.column} placeholder="列名" onChange={(e) => setField(i, { column: e.target.value })} />
                              {colErr ? <div className="xs" style={{ color: 'var(--red)', marginTop: 3 }}>{colErr}</div> : null}
                            </td>
                            <td><input className="cell" value={f.type} onChange={(e) => setField(i, { type: e.target.value })} /></td>
                            <td style={{ position: 'relative' }}>
                              <EvidenceInput
                                field={f} cell={fieldsByCol.get(f.column)} label={anchorLabels[f.evidence_anchor]} err={evErr}
                                onChange={(p) => setField(i, p)}
                                onPick={(anchor, label) => { setAnchorLabels((m) => ({ ...m, [anchor]: label })); setField(i, { evidence_anchor: anchor }); }}
                              />
                              <button type="button" className="rm" title="删除这个属性" style={{ position: 'absolute', right: 2, top: 10 }} onClick={() => change({ fields: form.fields.filter((_, j) => j !== i) })}><Icon name="x" size={12} /></button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              Object.entries(form.sections || {}).map(([title, text]) => (
                <div className="mt12" key={title}>
                  <Field label={title}><Area minHeight={96} textareaStyle={{ height: 78 }} value={text} onChange={(e) => change({ sections: { ...form.sections, [title]: e.target.value } })} /></Field>
                  {sectionRefs[title]?.length || reportRefs(text).length ? (
                    <div className="row wrap mt4" style={{ gap: 6 }}>
                      {reportRefs(text).map((r) => (
                        <Chip key={`${r.path}#${r.section}`} tone="line" sm icon="filetext" className="stt-src" title="打开资料分析报告" onClick={() => navigate(pagePath('reports', pid), { report: researchReportId(r.path), section: r.section })}>
                          {r.section}
                        </Chip>
                      ))}
                      {(sectionRefs[title] || []).map((a) => (
                        <Chip key={a} tone="line" sm icon="filetext" className="stt-src" title="查看原文" onClick={() => navigate(pagePath('resources', pid), { anchor: a })}>
                          {anchorLabels[a] || anchorChipLabel(a)}
                        </Chip>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))
            )}
            <div className="sec-t mt16">{isObject ? '关联对象' : '关联条目'}</div>
            <div className="row wrap" style={{ gap: 6 }}>
              {relRows.map((r) => (
                <span key={r.id} className="chip line">
                  <Icon name="link" />{r.name}
                  <span className="faint" style={{ marginLeft: 4, cursor: 'pointer' }} onClick={() => change({ relations: form.relations.filter((x) => x !== r.id) })}>×</span>
                </span>
              ))}
              <RelationPicker rows={rows.filter((r) => (isObject ? r.kind === 'object' : true))} exclude={exclude} onPick={(r) => change({ relations: [...(form.relations || []), r.id] })} />
            </div>
            {isObject ? (
              <div className="mt16">
                <Field label="数据来源">
                  <Inp
                    icon="db"
                    placeholder="不落库填「无」"
                    value={dsDraft ?? dsText(form, entry)}
                    onChange={(e) => setDsDraft(e.target.value)}
                    onBlur={() => { if (dsDraft != null) { change({ data_source: parseDs(dsDraft) }); setDsDraft(null); } }}
                  />
                </Field>
              </div>
            ) : null}
          </div>
          <div className="stt-ft">
            <span className="sp" />
            <Button kind="ghost" disabled={!hasDraft && !changes.length} onClick={() => setDiscardOpen(true)}>放弃草稿</Button>
            <Button kind="primary" icon="check" disabled={!!lockInfo || submitting} onClick={submit}>{submitting ? '检查中…' : '保存'}</Button>
          </div>
        </>
      )}
      <Modal
        open={discardOpen}
        onClose={() => setDiscardOpen(false)}
        icon="trash"
        danger
        title="放弃草稿？"
        w={440}
        foot={<><Button onClick={() => setDiscardOpen(false)}>继续编辑</Button><Button kind="danger" onClick={discard}>放弃</Button></>}
      >
        <div className="sm muted">{changes.length ? `本次草稿改了 ${changes.length} 处：${changes.join('、')}。` : '草稿和当前版本一样，没有改动。'}</div>
      </Modal>
    </div>
  );
}

