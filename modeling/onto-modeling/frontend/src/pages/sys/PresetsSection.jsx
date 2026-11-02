// 系统设置 · 模型上下文推荐表（SY-05～SY-07）：内置 / 覆盖 / 自定义，覆盖、编辑、删除、恢复内置、新增一行、导入、停用。
// 保存都是整份替换自定义表（SY-06，带读取时的 version）；校验失败的 details.error={row, column, message} 定位到行列。
import { useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Chip, Field, Inp, Modal, Note, Table,
} from '../../ui/index.jsx';
import { FieldErr, LoadNote, Loading, SysHead, useLoad } from './parts.jsx';
import {
  customPresets, filterPresets, fmtInt, importSummary, locatePresetError, parsePresetFile, presetEntry, presetErrorText,
  presetForm, presetFormProblems, presetRowActions, presetsNote, removePreset, upsertPreset,
} from './sysModel.js';

const ORIGIN_CHIP = { builtin: ['内置', 'gray'], override: ['覆盖', 'amber'], custom: ['自定义', 'blue'] };
const NOTE_ICON = { gray: 'info', red: 'alert' };

function EditCard({ f, setF, busy, problems, onCancel, onSave }) {
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  const title = f.mode === 'new' ? '新增一行' : `${f.mode === 'override' ? '覆盖' : '编辑'} ${f.replaced}`;
  const hint = (k, text) => (problems[k] ? <FieldErr>{problems[k]}</FieldErr> : text);
  const locked = f.mode !== 'new' && !!f.builtin;
  return (
    <Card title={title} icon="edit" className="mt16">
      <div className="grid4">
        <Field label="模型" hint={hint('model', null)}>
          <Inp mono value={f.model} readOnly={locked} error={!!problems.model} placeholder="model-…" onChange={set('model')} />
        </Field>
        <Field label="厂商"><Inp value={f.vendor} placeholder="厂商 A" onChange={set('vendor')} /></Field>
        <Field label="上下文窗口" hint={hint('window', f.builtin ? `内置 ${f.builtin.window}` : null)}>
          <Inp mono value={f.window} error={!!problems.window} onChange={set('window')} />
        </Field>
        <Field label="最大输出" hint={hint('max_output', f.builtin ? `内置 ${f.builtin.max_output}` : null)}>
          <Inp mono value={f.max_output} error={!!problems.max_output} onChange={set('max_output')} />
        </Field>
      </div>
      <div className="row mt12">
        <Field label="备注" className="grow" hint={problems.note ? <FieldErr>{problems.note}</FieldErr> : null}>
          <Inp value={f.note} onChange={set('note')} />
        </Field>
        <div style={{ alignSelf: 'flex-end' }} className="row">
          <Button kind="ghost" onClick={onCancel}>取消</Button>
          <Button kind="primary" disabled={busy} onClick={onSave}>保存</Button>
        </div>
      </div>
    </Card>
  );
}

export function PresetsSection() {
  const { api } = useApp();
  const { data, error, setData, reload } = useLoad(() => api.contextPresets());
  const [q, setQ] = useState('');
  const [edit, setEdit] = useState(null);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveErr, setSaveErr] = useState(null); // {error, list, source: 'edit'|'import', name?}
  const [imp, setImp] = useState(null); // {name, presets, summary}
  const [off, setOff] = useState(false);
  const fileRef = useRef(null);

  const rows = data?.rows || [];
  const invalid = data?.custom?.status === 'invalid';
  const lockHint = invalid ? '自定义表有误' : undefined;

  const saveList = async (list, okText, source, name) => {
    setBusy(true);
    setSaveErr(null);
    try {
      const v = await api.saveCustomContextPresets({ version: data.custom.version, presets: list });
      setData(v);
      toast(okText, 'check');
      return true;
    } catch (e) {
      if (e.isConflict) {
        toast('推荐表在别处被改过，已刷新，请再改一次', 'alert');
        reload();
      } else if (e.code === 'invalid_argument' && e.details?.error) {
        setSaveErr({ error: e.details.error, list, source, name });
      } else toast(errorText(e), 'alert');
      return false;
    } finally {
      setBusy(false);
    }
  };

  const problems = edit && tried ? presetFormProblems(edit, rows) : {};
  // 服务端校验失败、出错的正好是编辑的这一行：把错误挂到字段上
  const located = saveErr ? locatePresetError(saveErr.error, saveErr.list) : null;
  if (edit && saveErr?.source === 'edit' && located?.field && located.model === edit.model.trim() && !problems[located.field]) {
    problems[located.field] = saveErr.error.message;
  }
  const saveEdit = async () => {
    setTried(true);
    if (Object.keys(presetFormProblems(edit, rows)).length) return;
    const entry = presetEntry(edit);
    const list = upsertPreset(customPresets(rows), entry, edit.mode === 'new' ? null : edit.replaced);
    if (await saveList(list, '已保存', 'edit')) { setEdit(null); setTried(false); }
  };
  const startEdit = (row, mode) => { setEdit(presetForm(row, mode)); setTried(false); setSaveErr(null); };
  const restore = (row) => saveList(removePreset(customPresets(rows), row.model), `已恢复 ${row.model} 的内置值`, 'edit');
  const remove = (row) => saveList(removePreset(customPresets(rows), row.model), `已删除 ${row.model}`, 'edit');

  const pickFile = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    const text = await file.text();
    const r = parsePresetFile(file.name, text);
    if (r.error) { setSaveErr({ error: r.error, list: r.presets, source: 'import', name: file.name }); return; }
    setSaveErr(null);
    setImp({ name: file.name, presets: r.presets, summary: importSummary(r.presets, rows) });
  };
  const doImport = async () => {
    const { name, presets } = imp;
    setImp(null);
    await saveList(presets, `已导入「${name}」，共 ${presets.length} 行`, 'import', name);
  };
  const doDisable = async () => {
    setBusy(true);
    try {
      setData(await api.disableCustomContextPresets());
      setOff(false);
      setEdit(null);
      toast('自定义表已停用', 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  const note = data ? presetsNote(data) : null;
  const errText = saveErr ? `${presetErrorText(saveErr.error)}${located?.model ? `（${located.model}）` : ''}` : '';

  return (
    <>
      <SysHead title="模型上下文推荐表" />
      {!data ? (error ? <LoadNote error={error} what="上下文推荐表" onRetry={reload} /> : <Loading />) : (
        <>
          {note ? (
            <Note tone={note.tone} icon={NOTE_ICON[note.tone]}>
              {note.lead ? <b>{note.lead}</b> : null}{note.text}
            </Note>
          ) : null}
          {saveErr ? (
            <div className={note ? 'mt8' : undefined}>
              <Note tone="red">
                <b>{saveErr.source === 'import' ? `「${saveErr.name}」没有导入：` : '没有保存：'}</b>{errText}。
                {saveErr.source === 'import' ? '现有推荐表未变' : ''}
              </Note>
            </div>
          ) : null}
          <div className={note || saveErr ? 'row mt12 mb12' : 'row mb12'}>
            <Inp icon="search" placeholder="按模型或厂商筛选" w="260px" value={q} onChange={(e) => setQ(e.target.value)} />
            <span className="sp" />
            <Button size="sm" icon="plus" disabled={invalid || busy} title={lockHint} onClick={() => startEdit(null, 'new')}>新增一行</Button>
            <Button size="sm" kind="ghost" icon="upload" disabled={busy} onClick={() => fileRef.current?.click()} title="JSON 或 CSV，整份替换自定义表">导入</Button>
            <Button size="sm" kind="ghost" disabled={data.custom.status === 'disabled' || busy} onClick={() => setOff(true)}>停用自定义表</Button>
            <input ref={fileRef} type="file" accept=".json,.csv,application/json,text/csv" hidden onChange={pickFile} />
          </div>
          <Table
            cols={['模型', '厂商', '上下文窗口', '最大输出', '备注', '来源', '操作']}
            rows={filterPresets(rows, q).map((r) => {
              const [label, tone] = ORIGIN_CHIP[r.origin] || ORIGIN_CHIP.builtin;
              const acts = presetRowActions(r);
              const dis = invalid || busy;
              return [
                <span className="mono b" title={r.aliases?.length ? `别名：${r.aliases.join('、')}` : undefined}>{r.model}</span>,
                r.vendor || <span className="faint">—</span>,
                <span className="num" title={r.builtin ? `内置 ${fmtInt(r.builtin.window)}` : undefined}>{fmtInt(r.window)}</span>,
                <span className="num" title={r.builtin ? `内置 ${fmtInt(r.builtin.max_output)}` : undefined}>{fmtInt(r.max_output)}</span>,
                <span className="muted sm">
                  {r.note || '—'}
                  {(r.sources || []).map((u) => <a key={u} className="link sy-src" href={u} target="_blank" rel="noreferrer" title={u}>↗</a>)}
                </span>,
                <Chip tone={tone} sm>{label}</Chip>,
                <div className="row g4">
                  {acts.includes('override') ? <Button size="xs" disabled={dis} title={lockHint} onClick={() => startEdit(r, 'override')}>覆盖</Button> : null}
                  {acts.includes('edit') ? <Button size="xs" disabled={dis} title={lockHint} onClick={() => startEdit(r, 'edit')}>编辑</Button> : null}
                  {acts.includes('restore') ? <Button size="xs" kind="ghost" disabled={dis} title={lockHint} onClick={() => restore(r)}>恢复内置</Button> : null}
                  {acts.includes('delete') ? <Button size="xs" kind="ghost" disabled={dis} title={lockHint} onClick={() => remove(r)}>删除</Button> : null}
                </div>,
              ];
            })}
          />
          {!filterPresets(rows, q).length ? <div className="sm muted mt8">没有匹配「{q}」的模型。</div> : null}
          {edit ? (
            <EditCard f={edit} setF={setEdit} busy={busy} problems={problems} onCancel={() => { setEdit(null); setSaveErr(null); }} onSave={saveEdit} />
          ) : null}
        </>
      )}
      <Modal
        open={off} onClose={() => setOff(false)} icon="table" title="停用自定义推荐表？" w={480}
        foot={<><Button onClick={() => setOff(false)}>取消</Button><Button kind="danger" disabled={busy} onClick={doDisable}>停用</Button></>}
      >
        <div className="sm muted">已保存的模型上下文参数不受影响。</div>
      </Modal>
      <Modal
        open={!!imp} onClose={() => setImp(null)} icon="upload" title="导入自定义推荐表？" sub={imp?.name} w={480}
        foot={<><Button onClick={() => setImp(null)}>取消</Button><Button kind="primary" disabled={busy} onClick={doImport}>导入</Button></>}
      >
        {imp ? (
          <div className="sm muted">
            共 {imp.summary.total} 行（覆盖内置 {imp.summary.overrides}、新增 {imp.summary.added}），替换现有自定义表 {imp.summary.replacing} 行。
          </div>
        ) : null}
      </Modal>
    </>
  );
}
