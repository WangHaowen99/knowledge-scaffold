// Studio 表格与条目编辑（设计稿 studio-table）：左边条目表（ST-03，流程图行取 AR-02），右边条目编辑器或流程图面板；
// 新建（ST-09）。写入范围锁（CS-06）在表格里带锁标记，编辑器里出锁定条。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { ShellP } from '../../shell/Shell.jsx';
import { Button, Field, Inp, Modal, Seg } from '../../ui/index.jsx';
import '../../styles/studio.css';
import EntryEditor from './EntryEditor.jsx';
import FlowchartPanel from './FlowchartPanel.jsx';
import { KindChip, LockIcon, PlugMark, RowStatus, useLockCopy } from './parts.jsx';
import { filterRows, kindSegments, mergeFlowchartRows } from './studioModel.js';

const NEW_KINDS = [['object', '对象'], ['operation', '操作'], ['rule', '规则'], ['metric', '指标']];

function NewEntryDialog({ open, onClose, onCreated, flowchartEnabled }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [kind, setKind] = useState('object');
  const [name, setName] = useState('');
  const [key, setKey] = useState('');
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) { setName(''); setKey(''); setErr(null); } }, [open]);
  const create = async () => {
    if (!name.trim()) { setErr('名称不能为空'); return; }
    setBusy(true);
    try {
      const r = await api.createStudioEntry(pid, { kind, name: name.trim(), key: key.trim() || undefined, from_template: true });
      toast(`已新建「${name.trim()}」`, 'plus');
      onCreated(kind === 'flowchart' ? `flowchart:${r.key}` : r.id);
    } catch (e) {
      if (e.isConflict && e.details?.kind === 'name') setErr('已经有同名的条目，换个名字');
      else setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const kinds = flowchartEnabled ? [...NEW_KINDS, ['flowchart', <>流程图<PlugMark label="图示" /></>]] : NEW_KINDS;
  return (
    <Modal
      open={open}
      onClose={onClose}
      icon="plus"
      title="新建条目"
      sub="新建对象、操作、规则、指标或流程图，按模板生成后在右边继续编辑"
      w={480}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={busy} onClick={create}>新建</Button></>}
    >
      <div className="col" style={{ gap: 12 }}>
        <Field label="类型"><Seg items={kinds} value={kind} onChange={setKind} /></Field>
        <Field label="名称" req hint={err ? <span style={{ color: 'var(--red)' }}>{err}</span> : null}>
          <Inp error={!!err} autoFocus value={name} placeholder={kind === 'flowchart' ? '如：超量处理流程' : '如：门店等级'} onChange={(e) => { setName(e.target.value); setErr(null); }} />
        </Field>
        <Field label="Key" hint="英文标识，不填会自动生成"><Inp mono value={key} placeholder="如：store_level" onChange={(e) => setKey(e.target.value)} /></Field>
      </div>
    </Modal>
  );
}

export default function StudioTablePage({ route }) {
  const { api } = useApp();
  const { pid, hasArtifactType, subscribe } = useProject();
  const lockFor = useLockCopy();
  const [data, setData] = useState(null);
  const [kind, setKind] = useState('all');
  const [q, setQ] = useState(route.query?.q || '');
  const [newOpen, setNewOpen] = useState(false);
  const [tick, setTick] = useState(0);
  const flowchartEnabled = hasArtifactType('diagram.flowchart');

  const load = useCallback(async () => {
    try {
      const r = await api.studioEntries(pid, { limit: 500 });
      let artifacts = null;
      if (flowchartEnabled) {
        try { artifacts = (await api.artifacts(pid, 'diagram.flowchart', { limit: 200 })).items || null; } catch { artifacts = null; }
      }
      const rows = flowchartEnabled ? mergeFlowchartRows(r.items || [], artifacts) : (r.items || []).filter((x) => x.kind !== 'flowchart');
      const counts = { ...r.counts, flowchart: flowchartEnabled ? rows.filter((x) => x.kind === 'flowchart').length : null };
      counts.all = rows.length;
      setData({ rows, counts });
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  }, [api, pid, flowchartEnabled]);
  useEffect(() => { load(); }, [load, tick]);
  useEffect(() => subscribe((env) => { if (env.type === 'workspace.changed') setTick((t) => t + 1); }), [subscribe]);
  useEffect(() => { if (route.query?.q) setQ(route.query.q); }, [route.query?.q]);

  const rows = data?.rows || [];
  // 没指定条目时：先接着编辑自己有草稿的那条，没有就选第一条；选定后不随列表刷新跳走
  const [autoId, setAutoId] = useState('');
  useEffect(() => {
    if (!route.query?.entry && !autoId && rows.length) setAutoId((rows.find((r) => r.status === 'editing') || rows[0]).id);
  }, [route.query?.entry, autoId, rows]);
  const selectedId = route.query?.entry || autoId;
  const selected = rows.find((r) => r.id === selectedId) || null;
  const shown = useMemo(() => filterRows(rows, kind, q), [rows, kind, q]);
  const segs = kindSegments(data?.counts, { flowchartEnabled });
  const select = (id) => navigate(pagePath('studio-table', pid), { entry: id });
  // 从别处跳来（报告「定位」、对话里的条目链接）时，把选中的行滚进视野
  const bodyRef = useRef(null);
  useEffect(() => {
    const tr = bodyRef.current?.querySelector('tr.sel');
    tr?.scrollIntoView({ block: 'nearest' });
  }, [selectedId, data]);

  let right;
  if (!data) right = <div className="stt-r card"><div className="sd-empty">正在读取…</div></div>;
  else if (selectedId.startsWith('flowchart:') || selected?.kind === 'flowchart') {
    const key = selected?.key || selectedId.slice('flowchart:'.length);
    right = <FlowchartPanel key={key} row={selected || { key, name: key, kind: 'flowchart', status: 'confirmed' }} onDeleted={() => { navigate(pagePath('studio-table', pid)); setTick((t) => t + 1); }} onChanged={() => setTick((t) => t + 1)} />;
  } else if (selectedId) right = <EntryEditor key={selectedId} id={selectedId} rows={rows} tick={tick} onChanged={() => setTick((t) => t + 1)} />;
  else right = <div className="stt-r card"><div className="sd-empty">知识包里还没有条目，点「新建」开始</div></div>;

  return (
    <ShellP active="studio-table" bodyStyle={{ padding: '14px 18px 56px' }}>
      <div className="stt">
        <div className="stt-l card">
          <div className="card-h">
            <h3>知识包条目</h3>
            <span className="sub">共 {data?.counts?.all ?? 0} 个</span>
            <span className="sp" />
            <Button size="sm" icon="plus" data-pop="新建对象、操作、规则、指标或流程图" onClick={() => setNewOpen(true)}>新建</Button>
          </div>
          <div className="row" style={{ padding: '12px 16px 10px', gap: 10 }}>
            <Seg
              items={segs.map((s) => [s.k, <>{s.label} {s.n}{s.plugin ? <PlugMark label="图示" /> : null}</>])}
              value={kind}
              onChange={setKind}
            />
            <Inp className="grow" icon="search" placeholder="搜索元素名、Key" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="tw" style={{ padding: '0 16px 16px' }}>
            <div className="tbl-wrap">
              <table className="tbl compact">
                <thead><tr><th style={{ width: 74 }}>类型</th><th>名称</th><th style={{ width: 70 }}>引用</th><th style={{ width: 70 }}>被引</th><th style={{ width: 96 }}>状态</th></tr></thead>
                <tbody ref={bodyRef}>
                  {shown.map((r) => {
                    const ref = r.kind === 'flowchart' ? { type: 'diagram.flowchart', key: r.key } : { id: r.id };
                    return (
                      <tr key={r.id} className={`stt-row${r.id === selectedId ? ' sel' : ''}`} onClick={() => select(r.id)}>
                        <td><KindChip kind={r.kind} /></td>
                        <td className="b">{r.name}<LockIcon info={lockFor(ref, r.name)} /></td>
                        <td className="num">{r.refs_out}</td>
                        <td className="num">{r.refs_in}</td>
                        <td><RowStatus row={r} /></td>
                      </tr>
                    );
                  })}
                  {data && !shown.length ? <tr><td colSpan={5} className="muted sm" style={{ textAlign: 'center', padding: 24 }}>{rows.length ? '没有匹配的条目' : '还没有条目'}</td></tr> : null}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        {right}
      </div>
      <NewEntryDialog
        open={newOpen}
        flowchartEnabled={flowchartEnabled}
        onClose={() => setNewOpen(false)}
        onCreated={(id) => { setNewOpen(false); setTick((t) => t + 1); select(id); }}
      />
    </ShellP>
  );
}
