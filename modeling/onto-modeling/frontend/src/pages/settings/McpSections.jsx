// 项目设置 · 助手可用的 MCP 服务器（P-14）与对外 MCP 接口（M-01～M-04）：
// 新建 Token 的明文只在弹窗里出现一次，关掉就丢；客户端配置优先用服务端给的，没有时按端点在本地生成（lib/mcpClientConfig）
import { Fragment, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { buildClientConfigs } from '../../lib/mcpClientConfig.js';
import {
  Button, Check, Chip, Field, Icon, Inp, Modal, Note, Seg, Table, Toggle,
} from '../../ui/index.jsx';
import {
  SERVER_STATUS, TRANSPORT_LABEL, TTL_OPTIONS, defaultConfigTab, mdhm, packToolsText, primaryEndpoint, scopeLabel, scopeTone,
  tokenBody, tokenProblems, toolList, toolsText,
} from './settingsModel.js';

const CFG_TABS = [['r', 'Read 配置'], ['c', 'Change 配置'], ['f', '完整配置']];
const CFG_KEY = { r: 'read', c: 'change', f: 'full' };

async function copy(text, msg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(msg, 'copy');
  } catch {
    toast('复制失败，请手动选中复制', 'alert');
  }
}

export function McpServersSection({ d, put, archived }) {
  const { api, user } = useApp();
  const { pid } = useProject();
  const [busy, setBusy] = useState(false);
  const items = d.servers?.items || [];

  const toggle = async (srv, on) => {
    if (archived) { toast('项目已归档，只能查看', 'lock'); return; }
    const disabled = items.filter((x) => (x.name === srv.name ? !on : !x.enabled)).map((x) => x.name);
    setBusy(true);
    try {
      put('servers', await api.saveProjectMcpServers(pid, { disabled }));
      toast(on ? `已为本项目启用 ${srv.name}，新建的会话生效` : `已为本项目停用 ${srv.name}，新建的会话生效`, 'plug');
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const add = () => {
    if (user?.role === 'admin') navigate(pagePath('sys'), { section: 'mcp' });
    else toast('MCP 服务器由管理员在系统设置里添加；这里为本项目启用或停用', 'info');
  };

  return (
    <div className="card" data-sec="mcp-servers">
      <div className="card-h">
        <h3>助手可用的 MCP 服务器</h3><span className="sub">项目级</span><span className="sp" />
        <Button size="sm" icon="plus" onClick={add}>添加服务器</Button>
      </div>
      <div className="card-b">
        {d.servers ? (
          <Table
            compact
            cols={['名称', '传输', '地址 / 命令', '状态', '工具', '启用']}
            rows={items.map((s) => {
              const [label, tone] = SERVER_STATUS[s.status] || [s.status, 'gray'];
              return [
                <>{s.origin === 'builtin' ? <><b>{s.name}</b> <span className="chip gray sm">内置 · 只读</span></> : <b>{s.name}</b>}</>,
                TRANSPORT_LABEL[s.transport] || s.transport,
                <span className="mono sm">{s.target}</span>,
                <Chip tone={tone} sm dot>{label}</Chip>,
                toolsText(s.tools),
                <Toggle on={s.enabled} disabled={busy || !s.enabled_globally || archived} title={s.enabled_globally ? undefined : '管理员已在全局停用'} onChange={(v) => toggle(s, v)} />,
              ];
            })}
          />
        ) : <Note tone="red">服务器列表没有读出来，刷新页面再试。</Note>}
        <div className="xs muted mt8">变更只影响新建的会话；已在运行的会话保持原配置。</div>
      </div>
    </div>
  );
}

// 三份客户端配置：服务端给的模板（占位符）或按端点生成
function configsOf(mcp) {
  if (mcp?.client_config_templates) return mcp.client_config_templates;
  const runtime = { urls: mcp?.endpoints || [] };
  return {
    read: buildClientConfigs(runtime, '<READ_TOKEN>', ['ontology.read']).read,
    change: buildClientConfigs(runtime, '<CHANGE_TOKEN>', ['ontology.change.validate', 'ontology.change.apply']).change,
    full: buildClientConfigs(runtime, '<FULL_TOKEN>').combined,
  };
}

function ConfigCode({ text }) {
  return text ? <div className="code" style={{ fontSize: 11 }}>{text}</div> : <div className="ps-cfg-empty">这个 Token 没有这一组工具的权限</div>;
}

function TokenDialog({ mcp, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [form, setForm] = useState({ name: '', scopes: ['ontology.read', 'ontology.change.validate'], ttl: '30' });
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);
  const [tab, setTab] = useState('c');
  const problems = tried ? tokenProblems(form) : {};
  const catalog = mcp?.scope_catalog?.length ? mcp.scope_catalog : [];

  const create = async () => {
    setTried(true);
    if (Object.keys(tokenProblems(form)).length) return;
    setBusy(true);
    try {
      const body = tokenBody(form);
      const r = await api.createMcpToken(pid, body);
      const cfg = r.client_config || (() => {
        const all = buildClientConfigs({ urls: mcp?.endpoints || [] }, r.token, body.scopes);
        return { read: all.read, change: all.change, full: all.combined };
      })();
      setDone({ token: r.token, cfg });
      setTab(defaultConfigTab(body.scopes));
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const close = () => { setDone(null); onClose(!!done); };
  const toggleScope = (s) => setForm((f) => ({ ...f, scopes: f.scopes.includes(s) ? f.scopes.filter((x) => x !== s) : [...f.scopes, s] }));

  return (
    <Modal
      open onClose={close} sticky={!!done} icon="key" title="新建 MCP Token" sub="明文只会展示这一次" w={600}
      foot={done ? <Button kind="primary" onClick={close}>我已保存，关闭</Button> : (
        <><Button onClick={close}>取消</Button><Button kind="primary" icon="key" disabled={busy} onClick={create}>创建 Token</Button></>
      )}
    >
      {!done ? (
        <div>
          <Field label="Token 名称" req hint={problems.name ? <span style={{ color: 'var(--red)' }}>{problems.name}</span> : null}>
            <Inp placeholder="例如：业务 Agent 读写联调" value={form.name} error={!!problems.name} maxLength={130} autoFocus onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
          </Field>
          <div className="mt12">
            <Field label="权限范围" hint={<span style={problems.scopes ? { color: 'var(--red)' } : undefined}>至少选一个</span>}>
              <div className="col" style={{ gap: 8 }}>
                {catalog.map((c) => (
                  <Check key={c.scope} on={form.scopes.includes(c.scope)} onChange={() => toggleScope(c.scope)} label={<><b>{c.scope}</b> <span className="muted sm">{c.description}</span></>} />
                ))}
              </div>
            </Field>
          </div>
          <div className="mt12">
            <Field label="有效期">
              <Seg items={TTL_OPTIONS.map(([k, t]) => [k, t])} value={form.ttl} onChange={(v) => setForm((f) => ({ ...f, ttl: v }))} />
            </Field>
          </div>
        </div>
      ) : (
        <div>
          <Note tone="amber" icon="alert">Token 已创建。明文只回显这一次，关闭后无法再次查看。</Note>
          <div className="row mt12">
            <span className="anchor grow ps-tok">{done.token}</span>
            <Button size="sm" icon="copy" onClick={() => copy(done.token, '已复制 Token')}>复制</Button>
          </div>
          <div className="mt12">
            <div className="row mb8"><b className="sm">客户端 JSON 配置</b><span className="sp" /><Seg items={CFG_TABS} value={tab} onChange={setTab} /></div>
            <ConfigCode text={done.cfg[CFG_KEY[tab]]} />
            <div className="row mt4">
              <span className="xs muted grow">已按本次 Token 的权限范围生成；明文只在当前页面会话中保留。</span>
              {done.cfg[CFG_KEY[tab]] ? <a className="link xs" onClick={() => copy(done.cfg[CFG_KEY[tab]], '已复制客户端配置')}>复制配置</a> : null}
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}

export function McpOutSection({ d, reload }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [tab, setTab] = useState('r');
  const [dialog, setDialog] = useState(false);
  const [revoke, setRevoke] = useState(null);
  const mcp = d.mcp;
  const tokens = d.tokens?.items || [];
  const url = primaryEndpoint(mcp);
  const tools = toolList(mcp?.toolsets);
  const cfgs = configsOf(mcp);
  const order = mcp?.recommended_order || ['server/discover', 'tools/list', 'tools/call'];
  const limits = mcp?.limits || { page_size_max: 200, body_bytes: 1048576 };
  const mib = limits.body_bytes >= 1048576 ? `${Number((limits.body_bytes / 1048576).toFixed(1))} MiB` : `${Math.round(limits.body_bytes / 1024)} KiB`;

  const doRevoke = async () => {
    try {
      await api.revokeMcpToken(pid, revoke.id);
      toast('Token 已吊销', 'ban');
      setRevoke(null);
      reload('tokens');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  return (
    <div className="card" data-sec="mcp">
      <div className="card-h">
        <h3>对外 MCP 接口</h3><span className="sub">让外部 Agent 读写本项目的对象、操作、规则</span><span className="sp" />
        <Button size="sm" kind="primary" icon="plus" disabled={!mcp} onClick={() => setDialog(true)}>新建 Token</Button>
      </div>
      <div className="card-b">
        {!mcp ? <Note tone="red">对外 MCP 的信息没有读出来，刷新页面再试。</Note> : (
          <>
            <div className="row g12">
              <Chip tone={mcp.status === 'running' ? 'teal' : 'gray'} dot>{mcp.status === 'running' ? '运行中' : '已停用'}</Chip>
              <span className="anchor grow ell" style={{ fontSize: 12 }}>{url}</span>
              <Button size="xs" icon="copy" disabled={!url} onClick={() => copy(url, '已复制 MCP 地址')}>复制</Button>
            </div>
            {mcp.local_only ? <div className="mt8"><Note tone="gray" icon="info">当前 MCP 地址仅限本机访问。本机客户端可以直接用；远程访问时请用本工作台的可访问地址。</Note></div> : null}
            <div className="sec-t mt16">
              <Icon name="wrench" size={12} />可用工具 {tools.length} 个<span className="sp" />
              {mcp.pack_tools?.length ? <span className="faint">另有包贡献的只读工具：{packToolsText(mcp.pack_tools)}</span> : null}
            </div>
            <div className="evd-tools">{tools.map((t) => <span key={t}>{t}</span>)}</div>
            <div className="sec-t mt16"><Icon name="key" size={12} />现有 Token<span className="sp" /><span className="faint">只显示未吊销的；调用成功后更新最近使用时间</span></div>
            {tokens.length ? (
              <Table
                compact
                cols={['用途', '权限范围', '有效期', '创建', '最近使用', '']}
                rows={tokens.map((t) => [
                  <b>{t.name}</b>,
                  <div className="row g4 wrap">{t.scopes.map((s) => <Chip key={s} tone={scopeTone(s)} sm>{scopeLabel(s)}</Chip>)}</div>,
                  t.remaining_text,
                  `${t.issued_by} · ${mdhm(t.created_at)}`,
                  t.last_used_at ? mdhm(t.last_used_at) : <span className="faint">尚未使用</span>,
                  <Button size="xs" kind="danger" onClick={() => setRevoke(t)}>吊销</Button>,
                ])}
              />
            ) : <div className="sm muted">还没有 Token。外部 Agent 要先有一个 Token 才能连进来。</div>}
            <div className="grid2 mt16" style={{ gridTemplateColumns: '1.2fr 1fr' }}>
              <div>
                <div className="sec-t"><Icon name="code" size={12} />客户端 JSON 配置<span className="sp" /><Seg items={CFG_TABS} value={tab} onChange={setTab} /></div>
                <ConfigCode text={cfgs[CFG_KEY[tab]]} />
                <div className="xs muted mt4">&lt;…_TOKEN&gt; 是占位符，请替换为具备对应权限范围的 Token。</div>
              </div>
              <div>
                <div className="sec-t"><Icon name="flow" size={12} />外部 Agent 推荐连接顺序</div>
                <div className="evd-conn">
                  {order.map((s, i) => (
                    <Fragment key={s}>{i ? <Icon name="chev" size={14} /> : null}<span className="s"><b>{i + 1}</b>{s}</span></Fragment>
                  ))}
                </div>
                <div className="xs muted mt8">旧客户端如果仍走 initialize，当前服务也兼容。写入按版本号防覆盖，每页最多 {limits.page_size_max} 条，请求体不超过 {mib}。</div>
              </div>
            </div>
          </>
        )}
      </div>
      {dialog ? <TokenDialog mcp={mcp} onClose={(created) => { setDialog(false); if (created) reload('tokens'); }} /> : null}
      {revoke ? (
        <Modal
          open onClose={() => setRevoke(null)} icon="ban" danger title={`吊销「${revoke.name}」？`} sub="吊销后不能恢复" w={460}
          foot={<><Button onClick={() => setRevoke(null)}>取消</Button><Button kind="danger" className="fill" onClick={doRevoke}>吊销</Button></>}
        >
          <div className="sm muted">用这个 Token 的外部 Agent 会立刻连不上；还要用的话，新建一个 Token 换上。</div>
        </Modal>
      ) : null}
    </div>
  );
}
