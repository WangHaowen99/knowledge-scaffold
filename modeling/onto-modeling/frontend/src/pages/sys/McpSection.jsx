// 系统设置 · 全局 MCP 服务器（SY-12、SY-13）：列表、测试连接、新增 / 编辑 / 删除（整份替换），只影响新建的会话。
// 后端现状（B2 第 12 条）：状态不落盘恒为 unknown、不列工具，鉴权只支持 none；本页测过的结果只在页面里记着。
import { useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import {
  Button, Card, Chip, Field, Inp, Modal, Note, Seg, Select, Table,
} from '../../ui/index.jsx';
import { FieldErr, LoadNote, Loading, SysHead, useLoad } from './parts.jsx';
import {
  fmtHm, mcpForm, mcpItemFromForm, mcpItemsAfterRemove, mcpItemsAfterSave, mcpProblems, mcpSaveError, mcpStatus, mcpTarget, mcpTestText, mcpTools,
} from './sysModel.js';

const AUTH_OPTIONS = [{ value: 'none', label: '不鉴权' }, { value: 'bearer', label: 'Bearer Token（存引用）' }];

function ToolsCard({ server, tested }) {
  if (!server) {
    return <Card title="提供的工具" icon="terminal"><div className="sm muted">还没有全局 MCP 服务器。</div></Card>;
  }
  const t = tested[server.name];
  const tools = mcpTools(server, tested);
  const at = t?.at || server.last_test_at;
  const ms = t ? t.latency_ms : server.latency_ms;
  const sub = at ? `上次测试 ${fmtHm(at)}${ms != null ? ` · ${ms}ms` : ''}` : '还没测试过';
  let empty = '还没测试过。点上面的「测试连接」检查连通性。';
  if ((t && !t.ok) || (!t && server.status === 'failed')) empty = '连接失败，拿不到工具清单。';
  else if (t?.ok || server.status === 'connected') empty = '连接正常；服务端这一版只检查连通性，不列工具清单。';
  return (
    <Card title={`${server.name} 提供的工具`} sub={sub} icon="terminal">
      {tools.length ? (
        <Table
          compact
          cols={['工具', '说明', '只读']}
          rows={tools.map((x) => [
            <span className="mono">{x.name}</span>,
            x.description || <span className="faint">—</span>,
            x.read_only ? <Chip tone="teal" sm>是</Chip> : <Chip tone="amber" sm>否</Chip>,
          ])}
        />
      ) : <div className="sm muted">{empty}</div>}
    </Card>
  );
}

export function McpSection() {
  const { api } = useApp();
  const { data, error, setData, reload } = useLoad(() => api.globalMcpServers());
  const [tested, setTested] = useState({});
  const [sel, setSel] = useState(null);
  const [form, setForm] = useState(() => mcpForm(null));
  const [editing, setEditing] = useState(null);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [del, setDel] = useState(false);

  const items = data?.items || [];
  const shown = items.find((s) => s.name === sel) || items.find((s) => mcpTools(s, tested).length) || items[0] || null;
  const problems = tried ? mcpProblems(form, items, editing) : {};
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v && v.target ? v.target.value : v }));
  const reset = () => { setForm(mcpForm(null)); setEditing(null); setTried(false); };

  const testRow = async (s) => {
    setSel(s.name);
    try {
      const r = await api.testMcpServer({ server: mcpItemFromForm(mcpForm(s), s) });
      setTested((t) => ({ ...t, [s.name]: { ...r, at: new Date().toISOString(), code: r.ok ? null : String(r.error || r.detail || '').match(/\d{3}/)?.[0] || null } }));
      toast(mcpTestText(s.name, r), r.ok ? 'check' : 'alert');
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };
  const testForm = async () => {
    setTried(true);
    if (Object.keys(mcpProblems(form, items, editing)).length) return;
    try {
      const r = await api.testMcpServer({ server: mcpItemFromForm(form, items.find((x) => x.name === editing)) });
      toast(mcpTestText(form.name.trim(), r), r.ok ? 'check' : 'alert');
    } catch (e) {
      toast(mcpSaveError(e) || errorText(e), 'alert');
    }
  };
  const putAll = async (list, okText) => {
    setBusy(true);
    try {
      setData(await api.saveGlobalMcpServers({ items: list }));
      toast(okText, 'check');
      return true;
    } catch (e) {
      toast(mcpSaveError(e) || errorText(e), 'alert');
      return false;
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    setTried(true);
    if (Object.keys(mcpProblems(form, items, editing)).length) return;
    const item = mcpItemFromForm(form, items.find((x) => x.name === editing));
    if (await putAll(mcpItemsAfterSave(items, item, editing), '已保存，新会话生效')) {
      setTested((t) => { const n = { ...t }; delete n[item.name]; if (editing) delete n[editing]; return n; });
      setSel(item.name);
      reset();
    }
  };
  const remove = async () => {
    const name = editing;
    setDel(false);
    if (await putAll(mcpItemsAfterRemove(items, name), `已删除 ${name}，新会话生效`)) reset();
  };
  const startEdit = (s) => { setForm(mcpForm(s)); setEditing(s.name); setTried(false); setSel(s.name); };

  return (
    <>
      <SysHead
        title="MCP 服务器（全局）"
        desc="所有项目的助手都能用的外部工具。项目级的 MCP 在项目设置里单独配。"
        actions={<Button kind="primary" icon="plus" onClick={() => { reset(); document.getElementById('sy-mcp-name')?.focus(); }}>新增服务器</Button>}
      />
      <div className="mb12"><Note tone="gray" icon="info">变更只影响新建的会话，已运行的会话保持原配置。</Note></div>
      {!data ? (error ? <LoadNote error={error} what="MCP 服务器" onRetry={reload} /> : <Loading />) : (
        <>
          {items.length ? (
            <Table
              cols={['名称', '传输', '地址 / 命令', '状态', '工具', '操作']}
              rows={items.map((s) => {
                const [text, tone, icon] = mcpStatus(s, tested);
                const n = mcpTools(s, tested).length;
                return [
                  <span className="row g4">
                    <b className="sy-pick" onClick={() => setSel(s.name)}>{s.name}</b>
                    {s.enabled === false ? <Chip tone="gray" sm>已停用</Chip> : null}
                  </span>,
                  s.transport,
                  <span className="mono xs">{mcpTarget(s)}</span>,
                  <Chip tone={tone} icon={icon} sm>{text}</Chip>,
                  n ? String(n) : '—',
                  <div className="row g4">
                    <Button size="xs" onClick={() => testRow(s)}>测试连接</Button>
                    <Button size="xs" kind="ghost" onClick={() => startEdit(s)}>编辑</Button>
                  </div>,
                ];
              })}
            />
          ) : <div className="card pad sm muted">还没有全局 MCP 服务器。在下面新增一个，所有项目的助手都能用。</div>}
          <div className="grid2 mt16">
            <ToolsCard server={shown} tested={tested} />
            <Card title={editing ? `编辑 ${editing}` : '新增服务器'} icon={editing ? 'edit' : 'plus'}>
              <div className="col">
                <Field label="名称" hint={problems.name ? <FieldErr>{problems.name}</FieldErr> : null}>
                  <Inp id="sy-mcp-name" value={form.name} error={!!problems.name} placeholder="例如 kb-search" onChange={set('name')} />
                </Field>
                <div className="grid2">
                  <Field label="传输"><Seg items={[['http', 'http'], ['stdio', 'stdio']]} value={form.transport} onChange={set('transport')} /></Field>
                  <Field label="鉴权"><Select value={form.auth} options={AUTH_OPTIONS} onChange={set('auth')} /></Field>
                </div>
                {form.auth === 'bearer' ? (
                  <Field label="密钥引用" hint={problems.secret_ref ? <FieldErr>{problems.secret_ref}</FieldErr> : '只存引用，Token 本身放在密钥目录里'}>
                    <Inp mono value={form.secret_ref} error={!!problems.secret_ref} placeholder="cred://mcp/<名称>" onChange={set('secret_ref')} />
                  </Field>
                ) : null}
                <Field label="地址或命令" hint={problems.target ? <FieldErr>{problems.target}</FieldErr> : null}>
                  <Inp mono value={form.target} error={!!problems.target} placeholder="https://… 或 /opt/mcp/xxx --stdio" onChange={set('target')} />
                </Field>
                <div className="row">
                  {editing ? (
                    <>
                      <Button size="sm" kind="ghost" onClick={reset}>取消</Button>
                      <Button size="sm" kind="danger" disabled={busy} onClick={() => setDel(true)}>删除</Button>
                    </>
                  ) : null}
                  <span className="sp" />
                  <Button size="sm" onClick={testForm}>测试连接</Button>
                  <Button size="sm" kind="primary" disabled={busy} onClick={save}>保存</Button>
                </div>
              </div>
            </Card>
          </div>
        </>
      )}
      <Modal
        open={del} onClose={() => setDel(false)} icon="trash" danger title={`删除「${editing}」？`} sub="所有项目的新会话都不再带这个服务器" w={460}
        foot={<><Button onClick={() => setDel(false)}>取消</Button><Button kind="danger" className="fill" onClick={remove}>删除</Button></>}
      >
        <div className="sm muted">已在运行的会话保持原配置；项目设置里为它单独做的启用、停用也一并失效。</div>
      </Modal>
    </>
  );
}
