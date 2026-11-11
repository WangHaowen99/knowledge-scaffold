// 对外契约 · 服务令牌（X-06）：外部系统用它拉发布包（releases.read）、
// 可限定项目。明文只在创建响应里出现一次，弹窗里只留「我已保存，关闭」（同项目设置的 Token 弹窗）。
// 设计稿没画入口，暂不挂载（见 gaps.md F4）。
// 挂载方法：SysPage.jsx 的 PANES 加 contract: ServiceTokensSection，sysModel.js 的 SYS_SECTIONS 末尾加
// { key: 'contract', title: '对外契约', icon: 'key' }（契约 8.2「sys 页新增『对外契约』一节」）。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Check, Chip, Field, Inp, Modal, Note, Seg, Table,
} from '../../ui/index.jsx';
import { FieldErr, LoadNote, Loading, SysHead, useLoad } from './parts.jsx';
import {
  SERVICE_SCOPES, SERVICE_TTL, expiryText, fmtShort, projectScopeText, scopeShort, serviceTokenBody, serviceTokenProblems,
} from './sysModel.js';

const SCOPE_TONE = { 'releases.read': 'blue' };
const EMPTY = { name: '', scopes: ['releases.read'], limit: 'all', project_ids: [], ttl: '90' };

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制令牌', 'copy');
  } catch {
    toast('复制失败，请手动选中复制', 'alert');
  }
}

function CreateDialog({ projects, onClose }) {
  const { api } = useApp();
  const [f, setF] = useState(EMPTY);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);
  const problems = tried ? serviceTokenProblems(f) : {};
  const toggle = (k, v) => setF((x) => ({ ...x, [k]: x[k].includes(v) ? x[k].filter((y) => y !== v) : [...x[k], v] }));

  const create = async () => {
    setTried(true);
    if (Object.keys(serviceTokenProblems(f)).length) return;
    setBusy(true);
    try {
      const r = await api.createServiceToken(serviceTokenBody(f));
      setDone(r);
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const close = () => { const created = !!done; setDone(null); onClose(created); };

  return (
    <Modal
      open onClose={close} sticky={!!done} icon="key" title="新建服务令牌" sub="给评测服务等外部系统用；明文只会展示这一次" w={600}
      foot={done ? <Button kind="primary" onClick={close}>我已保存，关闭</Button> : (
        <><Button onClick={close}>取消</Button><Button kind="primary" icon="key" disabled={busy} onClick={create}>创建令牌</Button></>
      )}
    >
      {!done ? (
        <div className="col" style={{ gap: 12 }}>
          <Field label="用途" req hint={problems.name ? <FieldErr>{problems.name}</FieldErr> : null}>
            <Inp placeholder="例如：评测服务 · 拉包与回执" value={f.name} error={!!problems.name} maxLength={130} autoFocus onChange={(e) => setF((x) => ({ ...x, name: e.target.value }))} />
          </Field>
          <Field label="权限范围" hint={problems.scopes ? <FieldErr>{problems.scopes}</FieldErr> : '只能调发布列表、包清单、下载发布包、回执和反馈这几个接口'}>
            <div className="col" style={{ gap: 8 }}>
              {SERVICE_SCOPES.map(([k, t]) => (
                <Check key={k} on={f.scopes.includes(k)} onChange={() => toggle('scopes', k)} label={<><b className="mono">{k}</b> <span className="muted sm">{t}</span></>} />
              ))}
            </div>
          </Field>
          <Field label="项目" hint={problems.project_ids ? <FieldErr>{problems.project_ids}</FieldErr> : null}>
            <div><Seg items={[['all', '全部项目'], ['projects', '限定项目']]} value={f.limit} onChange={(v) => setF((x) => ({ ...x, limit: v }))} /></div>
            {f.limit === 'projects' ? (
              <div className="col mt8" style={{ gap: 6 }}>
                {projects.length ? projects.map((p) => (
                  <Check key={p.id} on={f.project_ids.includes(p.id)} onChange={() => toggle('project_ids', p.id)} label={<>{p.name} <span className="mono xs faint">{p.id}</span></>} />
                )) : <span className="sm muted">没有可选的项目。</span>}
              </div>
            ) : null}
          </Field>
          <Field label="有效期">
            <div><Seg items={SERVICE_TTL.map(([k, t]) => [k, t])} value={f.ttl} onChange={(v) => setF((x) => ({ ...x, ttl: v }))} /></div>
          </Field>
        </div>
      ) : (
        <div>
          <Note tone="amber" icon="alert">令牌已创建。明文只回显这一次，关闭后无法再次查看；交给外部系统时放在请求头 Authorization: Bearer 里。</Note>
          <div className="row mt12">
            <span className="anchor grow sy-tok">{done.token}</span>
            <Button size="sm" icon="copy" onClick={() => copy(done.token)}>复制</Button>
          </div>
          <div className="xs muted mt8">
            {done.summary?.name} · {(done.summary?.scopes || []).join('、')} · {projectScopeText(done.summary?.project_ids, projects)} · {expiryText(done.summary?.expires_at)}
          </div>
        </div>
      )}
    </Modal>
  );
}

export function ServiceTokensSection() {
  const { api } = useApp();
  const { data, error, reload } = useLoad(() => api.serviceTokens());
  const projects = useLoad(() => api.projects().then((r) => r.items || []));
  const [creating, setCreating] = useState(false);
  const [revoke, setRevoke] = useState(null);
  const items = data?.items || [];
  const plist = projects.data || [];

  const doRevoke = async () => {
    try {
      await api.revokeServiceToken(revoke.id);
      toast('服务令牌已吊销', 'ban');
      setRevoke(null);
      reload();
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  return (
    <>
      <SysHead
        title="对外契约"
        desc="评测服务等外部系统用服务令牌拉发布包、提交求解验证回执和外部反馈。令牌只能调这几个接口。"
        actions={<Button kind="primary" icon="plus" disabled={!data} onClick={() => setCreating(true)}>新建令牌</Button>}
      />
      <div className="mb12"><Note tone="gray" icon="info">令牌格式 onto_svc_…，服务端只存摘要；吊销后立即失效，不能恢复。</Note></div>
      {!data ? (error ? <LoadNote error={error} what="服务令牌" code="X-06" onRetry={reload} /> : <Loading />) : items.length ? (
        <Table
          cols={['用途', '权限范围', '项目', '有效期', '创建', '最近使用', '']}
          rows={items.map((t) => [
            <b>{t.name}</b>,
            <div className="row g4 wrap">{t.scopes.map((s) => <Chip key={s} tone={SCOPE_TONE[s] || 'gray'} sm title={scopeShort(s)}>{s}</Chip>)}</div>,
            <span className="sm">{projectScopeText(t.project_ids, plist)}</span>,
            expiryText(t.expires_at),
            <span className="sm">{t.created_by} · {fmtShort(t.created_at)}</span>,
            t.last_used_at ? fmtShort(t.last_used_at) : <span className="faint">尚未使用</span>,
            <Button size="xs" kind="danger" onClick={() => setRevoke(t)}>吊销</Button>,
          ])}
        />
      ) : <div className="card pad sm muted">还没有服务令牌。评测服务要先有一个令牌才能拉包、交回执。</div>}
      {creating ? <CreateDialog projects={plist} onClose={(created) => { setCreating(false); if (created) reload(); }} /> : null}
      {revoke ? (
        <Modal
          open onClose={() => setRevoke(null)} icon="ban" danger title={`吊销「${revoke.name}」？`} sub="吊销后不能恢复" w={460}
          foot={<><Button onClick={() => setRevoke(null)}>取消</Button><Button kind="danger" className="fill" onClick={doRevoke}>吊销</Button></>}
        >
          <div className="sm muted">用这个令牌的外部系统会立刻调不通；还要用的话，新建一个令牌换上。</div>
        </Modal>
      ) : null}
    </>
  );
}
