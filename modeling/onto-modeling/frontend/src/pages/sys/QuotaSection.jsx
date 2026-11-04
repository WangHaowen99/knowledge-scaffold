// 系统设置 · 配额与自主构建：按用户 / 按项目的默认配额（SY-15）、单独设置的增删（SY-16）、自主构建平台限制（SY-17）。
// 页头「保存」一次存两份（哪份改了存哪份），立即生效。哪组接口回 404 就不画那一块（R17；两组都没有时左栏不出这个分区）。
// 证据源相关的项（采集频率、快照体积、自主构建采集生产库）按证据源是否可用过滤（EV-01 读不到就不出），不只靠 not_enforced。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { evidenceFromSources } from '../../state/evidence.js';
import {
  Button, Card, Chip, Field, Icon, Inp, Modal, Seg, Select, Table, Toggle, cx,
} from '../../ui/index.jsx';
import { FieldErr, LoadNote, Loading, SysHead, useLoad } from './parts.jsx';
import {
  QUOTA_ITEMS, limitsBody, limitsDirty, limitsForm, limitsProblems, nightText, overrideBody, overrideProblems,
  quotaBody, quotaDirty, quotaForm, quotaItemLabel, quotaProblems, quotaValueText, subjectText,
} from './sysModel.js';

const EMPTY_OVERRIDE = { kind: 'user', id: '', item: 'daily_tokens', value: '', reason: '' };

function OverrideDialog({ existing, onClose, onAdded, hidden }) {
  const { api } = useApp();
  const [f, setF] = useState(EMPTY_OVERRIDE);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  // 选用户：用户列表（AD-01）读不出来时改成手填用户名
  const users = useLoad(() => api.adminUsers({ limit: 200 }));
  const userOptions = (users.data?.items || []).map((u) => ({ value: u.username, label: `${u.display_name || u.username}（${u.username}）` }));
  const problems = tried ? overrideProblems(f, existing) : {};
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v && v.target ? v.target.value : v }));
  const pickKind = (kind) => setF((x) => ({ ...x, kind, id: '', item: QUOTA_ITEMS[kind].some(([k]) => k === x.item) ? x.item : QUOTA_ITEMS[kind][0][0] }));
  const err = (k) => (problems[k] ? <FieldErr>{problems[k]}</FieldErr> : null);

  const add = async () => {
    setTried(true);
    if (Object.keys(overrideProblems(f, existing)).length) return;
    setBusy(true);
    try {
      const o = await api.addQuotaOverride(overrideBody(f));
      toast(`已为${subjectText(o.subject)}单独设置${quotaItemLabel(o.item)}，立即生效`, 'check');
      onAdded(o);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open onClose={onClose} icon="sliders" title="新增单独设置" w={520}
      foot={<><Button onClick={onClose}>取消</Button><Button kind="primary" disabled={busy} onClick={add}>添加</Button></>}
    >
      <div className="col" style={{ gap: 12 }}>
        <Field label="对象"><Seg items={[['user', '用户'], ['project', '项目']]} value={f.kind} onChange={pickKind} /></Field>
        {f.kind === 'user' && userOptions.length ? (
          <Field label="用户" hint={err('id')}>
            <Select value={f.id} options={[{ value: '', label: '选一个用户' }, ...userOptions]} onChange={set('id')} />
          </Field>
        ) : (
          <Field label={f.kind === 'user' ? '用户名' : '项目 ID'} hint={err('id')}>
            <Inp mono value={f.id} error={!!problems.id} placeholder={f.kind === 'user' ? '如 wanggong' : '如 parcel-sort-6c1f2a90'} onChange={set('id')} />
          </Field>
        )}
        <div className="grid2">
          <Field label="项" hint={err('item')}>
            <Select value={f.item} options={QUOTA_ITEMS[f.kind].filter(([k]) => !hidden.has(k)).map(([value, label]) => ({ value, label }))} onChange={set('item')} />
          </Field>
          <Field label="值" hint={err('value')}>
            <Inp mono value={f.value} error={!!problems.value} placeholder="如 5,000,000" onChange={set('value')} />
          </Field>
        </div>
        <Field label="原因" hint={err('reason')}>
          <Inp value={f.reason} error={!!problems.reason} placeholder="如 培训周临时放宽" onChange={set('reason')} />
        </Field>
      </div>
    </Modal>
  );
}

export function QuotaSection() {
  const { api } = useApp();
  const quota = useLoad(() => api.quotas());
  const limits = useLoad(() => api.autobuildLimits());
  const sources = useLoad(() => api.evidenceSources());
  const evidenceOn = evidenceFromSources(sources.data);
  const [qf, setQf] = useState(null);
  const [lf, setLf] = useState(null);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);

  // 表单跟着读到的数据初始化；保存后用返回值重置
  const qForm = qf || (quota.data ? quotaForm(quota.data) : null);
  const lForm = lf || (limits.data ? limitsForm(limits.data) : null);
  const qDirty = !!(quota.data && qf && quotaDirty(quota.data, qf));
  const lDirty = !!(limits.data && lf && limitsDirty(limits.data, lf));
  // SY-17 not_enforced：本期不生效的项（夜间优先、采集生产库）不显示，字段照常存
  const limitsOff = new Set(limits.data?.not_enforced || []);
  const qp = tried && qForm ? quotaProblems(qForm) : {};
  // 服务端判的字段错误（SY-17 回 422 fields.max_concurrent_runs=min 等）与本地校验一起显示
  const [srvLp, setSrvLp] = useState({});
  const lp = { ...srvLp, ...(tried && lForm ? limitsProblems(lForm) : {}) };
  const setQ = (k) => (e) => setQf({ ...qForm, [k]: e.target.value });
  const setL = (k) => (v) => { setSrvLp({}); setLf({ ...lForm, [k]: v && v.target ? v.target.value : v }); };
  const err = (p, k, text) => (p[k] ? <FieldErr>{p[k]}</FieldErr> : text);

  const save = async () => {
    setTried(true);
    if ((qDirty && Object.keys(quotaProblems(qForm)).length) || (lDirty && Object.keys(limitsProblems(lForm)).length)) return;
    setBusy(true);
    try {
      if (qDirty) { quota.setData(await api.saveQuotas(quotaBody(qForm))); setQf(null); }
      if (lDirty) { limits.setData(await api.saveAutobuildLimits(limitsBody(lForm, limits.data))); setLf(null); }
      setTried(false);
      toast('配额已保存，立即生效', 'check');
    } catch (e) {
      if (e.details?.fields?.max_concurrent_runs) setSrvLp({ max_concurrent_runs: '整数 1–16' });
      else toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const removeOverride = async (o) => {
    try {
      await api.deleteQuotaOverride(o.id);
      quota.setData((x) => ({ ...x, overrides: x.overrides.filter((y) => y.id !== o.id) }));
      toast(`已删除${subjectText(o.subject)}的单独设置`, 'check');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  const quotaMissing = quota.error?.status === 404;
  const limitsMissing = limits.error?.status === 404;
  const head = (
    <SysHead
      title="配额与自主构建"
      actions={<Button kind="primary" disabled={busy || (!qDirty && !lDirty)} onClick={save}>保存</Button>}
    />
  );

  const overrides = quota.data?.overrides || [];
  // 不显示的项：SY-15 not_enforced 列出的，加上证据源不可用时的采集频率、快照体积（字段保留、照常能存）
  const hidden = new Set([...(quota.data?.not_enforced || []), ...(evidenceOn ? [] : ['collections_per_source_per_hour', 'snapshot_git_mb'])]);
  return (
    <>
      {head}
      {quotaMissing ? null : !quota.data ? (quota.error ? <LoadNote error={quota.error} what="配额" onRetry={quota.reload} /> : <Loading />) : (
        <>
          <div className="grid2">
            <Card title="按用户（默认）" icon="user">
              <div className="col" style={{ gap: 12 }}>
                <Field label="同时在线会话" hint={err(qp, 'concurrent_sessions', null)}>
                  <Inp mono value={qForm.concurrent_sessions} error={!!qp.concurrent_sessions} onChange={setQ('concurrent_sessions')} />
                </Field>
                <Field label="每日 token 预算" hint={err(qp, 'daily_tokens', null)}>
                  <Inp mono value={qForm.daily_tokens} error={!!qp.daily_tokens} onChange={setQ('daily_tokens')} />
                </Field>
                {hidden.has('collections_per_source_per_hour') ? null : (
                  <Field label="证据采集频率" hint={err(qp, 'collections_per_source_per_hour', null)}>
                    <Inp value={qForm.collections_per_source_per_hour} error={!!qp.collections_per_source_per_hour} onChange={setQ('collections_per_source_per_hour')}
                      after={<span className="muted sm sy-nowrap">次 / 每个数据源每小时</span>} />
                  </Field>
                )}
              </div>
            </Card>
            <Card title="按项目" icon="pkg">
              <div className="col" style={{ gap: 12 }}>
                <Field label="每月 token 预算" hint={err(qp, 'monthly_tokens', null)}>
                  <Inp mono value={qForm.monthly_tokens} error={!!qp.monthly_tokens} onChange={setQ('monthly_tokens')} />
                </Field>
                {hidden.has('snapshot_git_mb') ? null : (
                  <Field label="单份快照在 git 里的上限" hint={err(qp, 'snapshot_git_mb', null)}>
                    <Inp mono value={qForm.snapshot_git_mb} error={!!qp.snapshot_git_mb} onChange={setQ('snapshot_git_mb')} after={<span className="muted sm">MB</span>} />
                  </Field>
                )}
              </div>
            </Card>
          </div>
          <Card title="单独设置" icon="sliders" className="mt16" extra={<Button size="xs" icon="plus" onClick={() => setAdding(true)}>新增</Button>}>
            {overrides.length ? (
              <Table
                compact
                cols={['对象', '项', '值', '原因', '']}
                rows={overrides.map((o) => [
                  <span title={o.created_by ? `${o.created_by} 设置` : undefined}>{subjectText(o.subject)}</span>,
                  quotaItemLabel(o.item),
                  <span className="num">{quotaValueText(o.item, o.value)}</span>,
                  o.reason || '—',
                  <Button size="xs" kind="ghost" onClick={() => removeOverride(o)}>删除</Button>,
                ])}
              />
            ) : <div className="sm muted">还没有单独设置</div>}
          </Card>
        </>
      )}
      {limitsMissing ? null : !limits.data ? (limits.error ? <div className="mt16"><LoadNote error={limits.error} what="自主构建限制" onRetry={limits.reload} /></div> : null) : (
        <div className={cx('card l4-border', !quotaMissing && 'mt16')}>
          <div className="card-h">
            <span style={{ color: 'var(--blue)' }}><Icon name="rocket" /></span>
            <h3>自主构建限制</h3><span className="sp" />
            <Chip tone="indigo" icon="bot" sm>当前 {limits.data.running ?? 0} 个运行中</Chip>
          </div>
          <div className="card-b">
            <div className="grid4">
              <Field label="同时运行上限" hint={err(lp, 'max_concurrent_runs', null)}>
                <Inp mono value={lForm.max_concurrent_runs} error={!!lp.max_concurrent_runs} onChange={setL('max_concurrent_runs')} />
              </Field>
            </div>
            {evidenceOn && !limitsOff.has('allow_production_collection') ? (
              <div className="pl-sw mt8">
                <div className="sm b">允许自主构建采集生产环境数据源</div>
                <Toggle on={lForm.allow_production_collection} onChange={setL('allow_production_collection')} />
              </div>
            ) : null}
            {limitsOff.has('night_priority') ? null : (
              <div className={cx('pl-sw', !evidenceOn && 'mt8')}>
                <div>
                  <div className="sm b">夜间优先</div>
                  <div className="xs muted">{nightText(limits.data.night_priority)}</div>
                </div>
                <Toggle on={lForm.night} onChange={setL('night')} />
              </div>
            )}
          </div>
        </div>
      )}
      {adding ? (
        <OverrideDialog
          existing={overrides}
          hidden={hidden}
          onClose={() => setAdding(false)}
          onAdded={(o) => { quota.setData((x) => ({ ...x, overrides: [...x.overrides, o] })); setAdding(false); }}
        />
      ) : null}
    </>
  );
}
