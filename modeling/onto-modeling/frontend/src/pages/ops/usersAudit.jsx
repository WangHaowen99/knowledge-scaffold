// 审计日志（O-48、O-49）与错误留档（O-50、O-51）。
// 运维、管理员在这里只看到路径、key、ID，不显示项目内容（契约 8.1 #29）；带 trace_id 的可以跳可观测查链路（O-04）。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Chip, Drawer, Empty, Icon, Inp, KV, Menu, Note, Seg, Table, useOutside,
} from '../../ui/index.jsx';
import {
  AUDIT_FILTERS, AUDIT_TYPE, ORIGIN_LABEL, actorLabel, auditContent, auditQuery, clockText, errorProject, errorSub, execText,
  exportName, mergePage, packNode, pagerText, traceTarget,
} from './usersModel.js';

export const AUDIT_PAGE = 50;

function goTrace(traceId) {
  const t = traceTarget(traceId);
  if (t) navigate(t.path, t.query);
}

function TraceLink({ traceId }) {
  if (!traceId) return null;
  return (
    <span className="ou-trace" title={`在可观测中查看链路 ${traceId}`} onClick={() => goTrace(traceId)} role="link">
      <Icon name="activity" size={12} />
    </span>
  );
}

function ExportButton({ onPick }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <Button size="sm" icon="download" onClick={() => setOpen((v) => !v)}>导出</Button>
      {open ? (
        <div style={{ position: 'absolute', top: 36, right: 0, zIndex: 5 }}>
          <Menu
            style={{ minWidth: 230 }}
            items={[
              { label: '导出 CSV（表格）', icon: 'download', fmt: 'csv' },
              { label: '导出 JSONL（完整字段）', icon: 'download', fmt: 'jsonl' },
            ]}
            onPick={(x) => { setOpen(false); onPick(x.fmt); }}
          />
        </div>
      ) : null}
    </div>
  );
}

export function AuditPane({ nameOf }) {
  const { api, services } = useApp();
  const [filter, setFilter] = useState('all');
  const [q, setQ] = useState('');
  const [qd, setQd] = useState('');
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loadErr, setLoadErr] = useState('');
  const req = useRef(0);

  useEffect(() => {
    const t = setTimeout(() => setQd(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  const load = useCallback(async (append, prev) => {
    const id = (req.current += 1);
    setBusy(true);
    try {
      const page = await api.ops.audit({ ...auditQuery(filter, qd, AUDIT_PAGE), cursor: append ? prev?.next_cursor : undefined });
      if (id !== req.current) return;
      setData((cur) => mergePage(append ? cur : null, page, { append }));
      setLoadErr('');
    } catch (e) {
      if (id === req.current) setLoadErr(errorText(e));
    } finally {
      if (id === req.current) setBusy(false);
    }
  }, [api, filter, qd]);

  useEffect(() => { load(false); }, [load]);

  async function exportAs(format) {
    try {
      await services.download('/audit/export', exportName(format), { ops: true, query: { ...auditQuery(filter, qd), format } });
      toast('已按当前筛选导出审计，已写审计');
      load(false);
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  }

  const items = data?.items || [];
  return (
    <div className="mt16">
      <div className="row mb12">
        <Seg items={AUDIT_FILTERS} value={filter} onChange={setFilter} id="opAudSeg" />
        <span className="sp" />
        <Inp icon="search" w="260px" placeholder="搜索操作者、项目或内容" value={q} onChange={(e) => setQ(e.target.value)} />
        <ExportButton onPick={exportAs} />
      </div>
      {loadErr ? <Note tone="red">{loadErr}</Note> : null}
      {data && !items.length ? (
        <div className="card" style={{ padding: 10 }}>
          <Empty icon="list" title="没有符合条件的审计记录">换个类型或关键词试试。</Empty>
        </div>
      ) : null}
      {items.length ? (
        <Table
          compact
          cols={['时间', '操作者', '类型', '内容', '来源 IP']}
          rows={items.map((e) => {
            const [label, tone] = AUDIT_TYPE[e.type] || [e.type, 'gray'];
            return [
              <span key="t" className="muted num">{clockText(e.ts)}</span>,
              <b key="a" className="sm">{actorLabel(e, nameOf)}</b>,
              <Chip key="k" tone={tone} sm>{label}</Chip>,
              <span key="c" className="sm">{auditContent(e)}</span>,
              <span key="i" style={{ whiteSpace: 'nowrap' }}><span className="mono xs muted">{e.ip || '—'}</span><TraceLink traceId={e.trace_id} /></span>,
            ];
          })}
        />
      ) : null}
      {data?.next_cursor ? (
        <div className="xs muted mt8">
          {pagerText(items.length, data.total)} · <Button size="xs" kind="ghost" disabled={busy} onClick={() => load(true, data)}>加载更多</Button>
        </div>
      ) : null}
      <div className="xs muted mt8">审计只追加不可编辑，保留 365 天。读请求不记录。</div>
    </div>
  );
}

export function ErrorsPane({ data, loadError, busy, onMore, onOpen }) {
  const items = data?.items || [];
  return (
    <div className="mt16">
      <Note tone="gray">用户只看到右侧那句友好提示，原始错误留在这里，并标出是哪个包、哪个节点出的错。点击一行查看完整堆栈。</Note>
      {loadError ? <Note tone="red" className="mt12">{loadError}</Note> : null}
      {data && !items.length ? (
        <div className="card mt12" style={{ padding: 10 }}>
          <Empty icon="check" title="没有错误留档">服务端出错时，原始错误会留在这里。</Empty>
        </div>
      ) : null}
      {items.length ? (
        // 点一行打开原始错误（Table 不带行点击，这里按行号委托）
        <div className="mt12 ou-errs" onClick={(ev) => { const tr = ev.target.closest('tbody tr'); if (tr && items[tr.rowIndex - 1]) onOpen(items[tr.rowIndex - 1]); }}>
          <Table
            compact
            cols={['时间', '项目', '来源', '包 / 节点', '原始错误', '用户看到的提示']}
            rows={items.map((e) => [
              <span key="t" className="muted num">{clockText(e.at)}</span>,
              <div key="p" className="row g4" style={{ whiteSpace: 'nowrap' }}><Icon name="lock" size={12} /><span className="sm">{errorProject(e)}</span></div>,
              <Chip key="o" tone="line" sm>{ORIGIN_LABEL[e.origin] || e.origin || '—'}</Chip>,
              <span key="k" className="mono xs">{packNode(e)}</span>,
              <a key="r" className="mono xs" style={{ color: 'var(--red)', cursor: 'pointer' }}>{e.raw_message}</a>,
              <span key="f" className="xs muted">{e.friendly_message}</span>,
            ])}
          />
          <div className="xs muted mt8" onClick={(ev) => ev.stopPropagation()}>
            {pagerText(items.length, data.total)}
            {data.next_cursor ? <> · <Button size="xs" kind="ghost" disabled={busy} onClick={onMore}>加载更多</Button></> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// 原始错误抽屉（O-51）：完整堆栈（服务端已脱敏）
export function ErrorDrawer({ row, onClose }) {
  const { api } = useApp();
  const [detail, setDetail] = useState(null);
  const [loadErr, setLoadErr] = useState('');
  useEffect(() => {
    let alive = true;
    setDetail(null);
    setLoadErr('');
    api.ops.error(row.id).then((d) => alive && setDetail(d)).catch((e) => alive && setLoadErr(errorText(e)));
    return () => { alive = false; };
  }, [api, row.id]);
  const e = detail || row;
  const trace = e.trace_id;

  async function copy() {
    try { await navigator.clipboard.writeText(detail?.stack || e.raw_message || ''); toast('已复制'); } catch { toast('复制失败，请手动选中复制', 'alert'); }
  }

  return (
    <Drawer
      open
      onClose={onClose}
      w={560}
      icon="alert"
      title="原始错误"
      sub={errorSub(e)}
      foot={(
        <>
          <Button size="sm" icon="copy" onClick={copy}>复制</Button>
          <Button size="sm" kind="primary" disabled={!trace} title={trace ? undefined : '这条错误没有记下链路 ID'} onClick={() => goTrace(trace)}>在可观测中查看链路</Button>
        </>
      )}
    >
      <KV pairs={[
        ['trace id', trace ? <span className="mono">{trace}</span> : '—'],
        ['执行', execText(detail?.context)],
        ['节点', e.node ? <span className="mono">{e.node}</span> : '—'],
        ['自动处理', detail?.auto_action || '—'],
      ]}
      />
      {loadErr ? <Note tone="red" className="mt12">{loadErr}</Note> : null}
      {detail || !loadErr || e.raw_message ? <div className="code mt12">{detail ? detail.stack || e.raw_message : loadErr ? e.raw_message : '正在读取…'}</div> : null}
    </Drawer>
  );
}
