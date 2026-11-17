// 版本清单（O-42）、数据保留（O-43、O-44）、诊断包（O-45～O-47）
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import {
  Button, Card, Check, Icon, Inp, Modal, Note, Table,
} from '../../ui/index.jsx';
import { Missing } from './ctlParts.jsx';
import {
  DIAG_ITEMS, INV_CATEGORY, diagDefaults, diagLine, diagNote, fmtBytes, fmtDay, retentionBody, retentionForm, retentionText,
  validateRetention,
} from './ctlModel.js';

export function VersionsCard({ versions }) {
  return (
    <Card title="版本清单" icon="layers" data-sec="versions">
      <Table
        compact
        cols={['类别', '组件', '版本']}
        rows={(versions?.items || []).map((r) => [
          <span className="muted" style={{ whiteSpace: 'nowrap' }}>{INV_CATEGORY[r.category] || r.category}</span>,
          <span className="sm" title={r.note || undefined}>{r.component}</span>,
          <span className="mono xs">{r.version}</span>,
        ])}
      />
      {!versions ? <Missing /> : null}
    </Card>
  );
}

function RetentionModal({ open, onClose, items, run, busy, onSaved }) {
  const { api } = useApp();
  const [form, setForm] = useState(() => retentionForm(items));
  useEffect(() => { if (open) setForm(retentionForm(items)); }, [open, items]);
  const errors = validateRetention(form);
  const body = retentionBody(form, items);
  const save = () => run('retention', () => api.ops.saveRetention(body), { ok: '已保存保留期' })
    .then((r) => { if (r) { onSaved(r); onClose(); } });
  return (
    <Modal
      open={open} onClose={onClose} icon="clock" title="编辑数据保留" w={560}
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={!body.items.length || Object.keys(errors).length > 0 || busy === 'retention'} onClick={save}>保存</Button>
        </>
      )}
    >
      <div className="col" style={{ gap: 8 }}>
        {form.map((f, i) => (
          <div key={f.id} className="row oc-ret">
            <b className="sm" style={{ width: 92 }}>{f.label}</b>
            {f.editable ? (
              <Inp
                w="120px" value={f.value} error={!!errors[f.id]} inputMode="numeric"
                onChange={(e) => setForm((x) => x.map((y, j) => (j === i ? { ...y, value: e.target.value.replace(/[^\d]/g, '') } : y)))}
                after={<span className="faint xs">{f.unit === 'days' ? '天' : '份'}</span>}
              />
            ) : (
              <span className="op-lock" style={{ width: 120 }}><Icon name="lock" size={12} />{f.value} 天</span>
            )}
            <span className="xs muted grow">
              {errors[f.id] || (!f.editable ? '只追加，不可编辑' : '')}
            </span>
          </div>
        ))}
      </div>
    </Modal>
  );
}

export function RetentionCard({ retention, perm, run, busy, onSaved }) {
  const [open, setOpen] = useState(false);
  const items = retention?.items || [];
  return (
    <Card
      title="数据保留" icon="clock" data-sec="retention"
      extra={<Button size="xs" icon="edit" disabled={!perm.opsWrite || !items.length} title={perm.opsWrite ? null : perm.hint('ops.write')} onClick={() => setOpen(true)}>编辑</Button>}
    >
      <Table
        compact
        cols={['数据', '保留期', '清理规则', '下次清理']}
        rows={items.map((r) => [
          <b className="sm" style={{ whiteSpace: 'nowrap' }}>{r.label}</b>,
          retentionText(r),
          <span className="muted sm">{r.rule}</span>,
          <span className="muted">{r.next_run_at ? fmtDay(r.next_run_at) : '—'}</span>,
        ])}
      />
      {!retention ? <Missing /> : null}
      <RetentionModal open={open} onClose={() => setOpen(false)} items={items} run={run} busy={busy} onSaved={onSaved} />
    </Card>
  );
}

export function DiagnosticsCard({ diags, names, perm, run, busy, reload }) {
  const { api, services } = useApp();
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState(diagDefaults);
  const items = diags?.items || [];
  const latest = items[0] || null;
  useEffect(() => { if (open) setPick(diagDefaults()); }, [open]);
  const make = () => run('diag', () => api.ops.createDiagnostics({ include: pick }), {
    ok: (r) => `诊断包已生成：${r.name}（${fmtBytes(r.size)}）`, icon: 'download',
  }).then((r) => { if (r) { reload('diags'); setOpen(false); } });
  const download = (d) => run(`dl:${d.id}`, () => services.download(`/diagnostics/${encodeURIComponent(d.id)}/download`, d.name, { ops: true }), {
    ok: `已开始下载 ${d.name}`, icon: 'download',
  });
  return (
    <Card
      title="诊断包" icon="download" data-sec="diagnostics"
      extra={<Button size="xs" kind="primary" icon="download" disabled={!perm.opsWrite} title={perm.opsWrite ? null : perm.hint('ops.write')} onClick={() => setOpen(true)}>生成诊断包</Button>}
    >
      {items.slice(0, 3).map((d, i) => (
        <div key={d.id} className={i ? 'row mt12 sm' : 'row sm'}>
          <span className="mono xs muted grow">{diagLine(d, names)}</span>
          <Button size="xs" kind="ghost" icon="download" disabled={busy === `dl:${d.id}`} onClick={() => download(d)}>下载</Button>
        </div>
      ))}
      {diags && !items.length ? <div className="xs muted">还没有生成过诊断包。</div> : null}
      {!diags ? <Missing /> : null}
      <Modal
        open={open} onClose={() => setOpen(false)} icon="download" title="导出诊断包" sub="已脱敏，不含密钥和项目内容"
        foot={(
          <>
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button kind="primary" icon="download" disabled={!pick.length || busy === 'diag'} title={pick.length ? null : '至少选一项'} onClick={make}>
              {busy === 'diag' ? '正在打包…' : '生成诊断包'}
            </Button>
          </>
        )}
      >
        {DIAG_ITEMS.map(([k, label]) => (
          <div key={k} className="op-cb">
            <Check on={pick.includes(k)} label={label} onChange={(on) => setPick((x) => (on ? [...x, k] : x.filter((y) => y !== k)))} />
          </div>
        ))}
        {latest ? <div className="mt8"><Note tone="gray" icon="clock">{diagNote(latest, names)}</Note></div> : null}
      </Modal>
    </Card>
  );
}
