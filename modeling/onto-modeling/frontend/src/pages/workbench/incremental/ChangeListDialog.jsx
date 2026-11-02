// 变更点清单弹窗（设计稿 incremental 的 inChM）：逐条核对条件、取值和范围。
// 「接受」立即生效（INC-03，把握低的只能单独接受）；改写和补充先在弹窗里改，「保存清单」整份提交（INC-02），保存后影响面重算。
// 清单执行以后只能查看。
import { useCallback, useEffect, useState } from 'react';
import { useApp, useProject } from '../../../app/context.js';
import { toast } from '../../../app/toast.jsx';
import { errorText } from '../../../api/errors.js';
import { Button, Chip, Ev, Icon, Modal, cx } from '../../../ui/index.jsx';
import { Confidence } from '../cards-incremental.jsx';
import * as M from './model.js';

export default function ChangeListDialog({ id, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [cl, setCl] = useState(null);
  const [rows, setRows] = useState([]);
  const [editing, setEditing] = useState(() => new Set());
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await api.changeList(pid, id);
    setCl(r);
    setRows(M.toRows(r.points));
    setEditing(new Set());
    setError('');
  }, [api, pid, id]);
  useEffect(() => {
    load().catch((e) => { toast(errorText(e), 'alert'); onClose(); });
  }, [load]); // eslint-disable-line react-hooks/exhaustive-deps

  const readOnly = cl?.state === 'executed';
  const setRow = (key, patch) => setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const edit = (key, on) => setEditing((s) => {
    const n = new Set(s);
    if (on) n.add(key); else n.delete(key);
    return n;
  });

  // 接受：服务端立即生效，本地的改写不动
  const accept = async (r) => {
    setBusy(true);
    try {
      const next = await api.acceptChangePoints(pid, id, { point_ids: [r.id] });
      setCl(next);
      setRows((rs) => rs.map((x) => {
        const p = next.points.find((y) => y.id === x.id);
        return p ? { ...x, state: p.state } : x;
      }));
      toast(`已接受第 ${r.n} 条`, 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const saveRow = (r) => {
    const bad = M.validateRow(r);
    if (bad) { setError(`第 ${r.n} 行：${bad}`); return; }
    setError('');
    edit(r.key, false);
  };
  const cancelRow = (r) => {
    if (!r.origin) setRows((rs) => rs.filter((x) => x.key !== r.key).map((x, i) => ({ ...x, n: i + 1 })));
    else setRow(r.key, r.origin);
    setError('');
    edit(r.key, false);
  };
  const addRow = () => {
    const r = M.newRow(rows);
    setRows((rs) => [...rs, r]);
    edit(r.key, true);
  };

  const save = async () => {
    if (!M.isDirty(rows)) { onClose(); return; }
    const bad = rows.map((r) => [r, M.validateRow(r)]).find(([, e]) => e);
    if (bad) { setError(`第 ${bad[0].n} 行：${bad[1]}`); edit(bad[0].key, true); return; }
    setBusy(true);
    try {
      const r = await api.saveChangeList(pid, id, { base_revision: cl.revision, points: M.toInputs(rows) });
      toast('已保存，影响面已重算', 'check');
      onClose();
    } catch (e) {
      toast(errorText(e), 'alert');
      if (e.isConflict) load().catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const cell = (r, field) => {
    if (editing.has(r.key)) {
      return (
        <div className="inp">
          <input value={r[field]} placeholder={field === 'condition' ? '条件' : field === 'value' ? '取值' : '范围'} onChange={(e) => setRow(r.key, { [field]: e.target.value })} onKeyDown={(e) => { if (e.key === 'Enter') saveRow(r); }} />
        </div>
      );
    }
    return readOnly ? r[field] : <span className="inc-cell" title="点击修改" onClick={() => edit(r.key, true)}>{r[field] || <span className="faint">—</span>}</span>;
  };
  const act = (r) => {
    if (editing.has(r.key)) {
      return (
        <div className="row g4">
          <Button size="xs" kind="primary" icon="check" title="保存这一行" onClick={() => saveRow(r)} />
          <Button size="xs" icon="x" title="取消" onClick={() => cancelRow(r)} />
        </div>
      );
    }
    if (!r.id) return <Chip tone="amber" sm>新增</Chip>;
    if (M.rowChanged(r)) return <Chip tone="amber" sm>已修改</Chip>;
    if (M.isDone(r)) return <Chip tone="blue" sm icon="check">{r.state === 'rewritten' ? '已改写' : '已接受'}</Chip>;
    if (readOnly) return null;
    if (r.confidence === 'low') {
      return (
        <div className="row g4">
          <Button size="xs" icon="edit" onClick={() => edit(r.key, true)}>改写</Button>
          <Button size="xs" kind="soft" icon="check" title="接受" disabled={busy} onClick={() => accept(r)} />
        </div>
      );
    }
    return <Button size="xs" kind="soft" disabled={busy} onClick={() => accept(r)}>接受</Button>;
  };

  const source = cl ? M.sourceName(cl.source) : '';
  const note = M.dialogNote(rows, editing);
  return (
    <Modal
      open onClose={onClose} icon="list" w={1080} className="inc-dlg"
      title={`变更点清单${source ? ` · ${source}` : ''}`}
      sub={readOnly ? '已执行，只读' : undefined}
      foot={readOnly ? <><span className="sp" /><Button onClick={onClose}>关闭</Button></> : (
        <>
          <Button kind="ghost" icon="plus" onClick={addRow}>补充一条</Button>
          <span className="sp" />
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={busy || !cl} onClick={save}>保存清单</Button>
        </>
      )}
    >
      {!cl ? <div className="row muted sm"><span className="spin" />正在读取清单…</div> : (
        <>
          <div className="tbl-wrap">
            <table className="tbl compact">
              <thead>
                <tr>
                  <th style={{ width: 28 }}>#</th><th>条件</th><th>取值</th><th style={{ width: 150 }}>范围</th>
                  <th style={{ width: 70 }}>出处</th><th style={{ width: 60 }}>把握</th><th style={{ width: 92 }} />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.key} className={cx(editing.has(r.key) ? 'mr-edit' : r.confidence === 'low' && 'mr-low')}>
                    <td>{r.n}</td>
                    <td>{cell(r, 'condition')}</td>
                    <td>{cell(r, 'value')}</td>
                    <td>{cell(r, 'scope')}</td>
                    <td>{r.anchor ? <Ev anchor={r.anchor} label={M.shortAnchor(r.anchor_label)} /> : <span className="xs faint">{r.anchor_label || '手动补充'}</span>}</td>
                    <td>{r.confidence ? <Confidence p={r} mark={false} /> : <span className="xs faint">—</span>}</td>
                    <td>{act(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!readOnly && note ? <div className="row mt12 sm muted g4"><Icon name="info" size={14} />{note}</div> : null}
          {error ? <div className="row mt4 sm g4" style={{ color: 'var(--red)' }}><Icon name="alert" size={14} />{error}</div> : null}
        </>
      )}
    </Modal>
  );
}
