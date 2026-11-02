// 管理员审核申请（AD-08～AD-10）：插件安装（PL-04）、数据源登记（EV-03）、知识提名（K-08）。
// 设计稿没画入口，暂不挂载（见 gaps.md F4）。契约 8.2 建议入口：通知进入；plugin-upload 加「申请」页签。
// 挂载方法：PluginUploadPage.jsx 的 UploadReview 里加 const [tab, setTab] = useState(route.query?.tab === 'req' ? 'req' : 'up')，
// PageHead 下面放 <Tabs items={[['up', '上传', null, 'upload'], ['req', '申请', null, 'inbox']]} value={tab} onChange={setTab} />，
// tab === 'req' 时把 .plu-grid 换成 <AdminRequests />；通知 request_pending 的跳转写 plugin-upload?tab=req。
// 版式与上传页的卡片一致（.card、.pl-file 行、.pl-ico 图标底色）。只有管理员能调这组接口。
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Area, Button, Chip, Empty, Field, Icon, KV, Modal, Note, Seg, cx,
} from '../../ui/index.jsx';
import '../../styles/plugins-upload.css';
import {
  REQUEST_KINDS, REQUEST_STATES, approveHint, filterRequests, reasonProblem, requestCounts, requestMeta, requestReason, requestToast,
} from './uploadModel.js';
import { useActorNames } from './uploadParts.jsx';

const ENV_LABEL = { production: '生产环境', test: '测试环境', staging: '预发环境' };

// 申请里要给管理员看的内容（按类型）
function RequestDetail({ r }) {
  const p = r.payload || {};
  if (r.kind === 'knowledge_nomination') {
    return p.sanitized_text ? <div className="note gray mt12" style={{ display: 'block' }}><div className="xs muted mb8">脱敏后的条目原文</div>{p.sanitized_text}</div> : null;
  }
  if (r.kind === 'source_registration') {
    const c = p.connection || {};
    return (
      <KV w="70px" style={{ marginTop: 12 }} pairs={[
        ['提供方', p.provider || '—'],
        ['环境', ENV_LABEL[p.environment] || p.environment || '—'],
        ['连接', [c.host, c.port, c.database].filter(Boolean).join(' · ') || '—'],
      ]} />
    );
  }
  if (r.kind === 'pack_install') return <KV w="70px" style={{ marginTop: 12 }} pairs={[['插件', `${p.pack?.label || p.pack?.name || '—'} ${p.version || ''}`.trim()]]} />;
  return null;
}

function DecideModal({ r, action, busy, nameOf, onClose, onSubmit }) {
  const [text, setText] = useState('');
  const approve = action === 'approve';
  const problem = approve ? null : reasonProblem(text);
  const kind = REQUEST_KINDS[r.kind];
  return (
    <Modal
      open w={540} icon={approve ? 'check' : 'undo'} danger={!approve} onClose={onClose}
      title={`${approve ? '通过' : '退回'}${kind?.label || ''}申请`} sub={r.summary}
      foot={(
        <>
          <Button onClick={onClose}>取消</Button>
          <Button kind={approve ? 'primary' : 'danger'} icon={approve ? 'check' : 'undo'} disabled={busy || !!problem} title={problem || undefined}
            onClick={() => onSubmit(text.trim())}>{approve ? '通过' : '退回'}</Button>
        </>
      )}
    >
      <KV w="70px" pairs={[
        ['申请人', nameOf(r.requested_by, false)],
        ['项目', r.payload?.project_name || r.project_id || '—'],
        ['理由', requestReason(r) || '—'],
      ]} />
      <RequestDetail r={r} />
      {approve && approveHint(r) ? <Note tone="amber" className="mt12">{approveHint(r)}</Note> : null}
      <Field className="mt12" label={approve ? '备注（可选）' : '退回原因'} req={!approve} hint="写进审核记录。">
        <Area value={text} onChange={(e) => setText(e.target.value)} minHeight={approve ? 64 : 88} />
      </Field>
    </Modal>
  );
}

function RequestRow({ r, nameOf, onDecide }) {
  const kind = REQUEST_KINDS[r.kind] || { label: r.kind, icon: 'inbox', cls: 'exp' };
  const [stateText, stateTone] = REQUEST_STATES[r.state] || [r.state, 'gray'];
  const why = requestReason(r);
  return (
    <div className="pl-file plu-req">
      <span className={cx('pl-ico', kind.cls)}><Icon name={kind.icon} size={16} /></span>
      <div className="grow">
        <div className="row g4"><span className="b sm">{r.summary}</span><Chip tone="line" sm>{kind.label}</Chip></div>
        <div className="xs muted">{requestMeta(r, { nameOf: (x) => nameOf(x, false) })}</div>
        {why ? <div className="why">{why}</div> : null}
        {r.state !== 'pending' && r.note ? <div className="xs faint mt4">{r.state === 'approved' ? '备注' : '退回原因'}：{r.note}</div> : null}
      </div>
      {r.state === 'pending' ? (
        <div className="row g4">
          <Button size="sm" onClick={() => onDecide(r, 'reject')}>退回</Button>
          <Button size="sm" kind="primary" icon="check" onClick={() => onDecide(r, 'approve')}>通过</Button>
        </div>
      ) : <Chip tone={stateTone} sm>{stateText}</Chip>}
    </div>
  );
}

export default function AdminRequests() {
  const { api } = useApp();
  const [items, setItems] = useState(null);
  const [kind, setKind] = useState('all');
  const [state, setState] = useState('pending');
  const [deciding, setDeciding] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api.adminRequests({ limit: 200 })
    .then((r) => setItems(r.items || []))
    .catch((e) => { setItems((x) => x || []); toast(errorText(e), 'alert'); }), [api]);
  useEffect(() => { load(); }, [load]);

  const counts = useMemo(() => requestCounts(items), [items]);
  const shown = useMemo(() => filterRequests(items, { kind, state }), [items, kind, state]);
  const nameOf = useActorNames((items || []).map((r) => r.requested_by));

  const submit = async (text) => {
    const { r, action } = deciding;
    setBusy(true);
    try {
      if (action === 'approve') await api.approveRequest(r.id, text ? { note: text } : {});
      else await api.rejectRequest(r.id, { reason: text });
      toast(requestToast(r, action), action === 'approve' ? 'check' : 'undo');
      setDeciding(null);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
      load();
    }
  };

  const n = (k) => (counts[k] ? ` ${counts[k]}` : '');
  return (
    <div className="card">
      <div className="card-h">
        <h3>审核申请</h3>
        <span className="sub">建模者提交，管理员通过后生效</span>
        <span className="sp" />
        <Seg items={[['pending', `待处理${n('all')}`], ['done', '已处理']]} value={state} onChange={setState} />
      </div>
      <div className="card-b">
        <Seg
          items={[['all', '全部'], ...Object.entries(REQUEST_KINDS).map(([k, v]) => [k, `${v.label}${state === 'pending' ? n(k) : ''}`])]}
          value={kind} onChange={setKind}
        />
        {items === null ? <div className="muted sm mt12">正在读取申请…</div> : shown.length ? (
          <div className="col mt12" style={{ gap: 8 }}>
            {shown.map((r) => <RequestRow key={r.id} r={r} nameOf={nameOf} onDecide={(x, action) => setDeciding({ r: x, action })} />)}
          </div>
        ) : (
          <Empty icon="inbox" title={state === 'pending' ? '没有待处理的申请' : '还没有处理过的申请'}>
            插件安装、数据源登记和知识提名的申请会出现在这里，有新申请时铃铛里也会提醒。
          </Empty>
        )}
      </div>
      {deciding ? (
        <DecideModal r={deciding.r} action={deciding.action} busy={busy} nameOf={nameOf} onClose={() => setDeciding(null)} onSubmit={submit} />
      ) : null}
    </div>
  );
}
