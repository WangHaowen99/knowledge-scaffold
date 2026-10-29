import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, TOKEN, REFRESH, saveLogin, clearLogin, requestId } from './api.js';
import { receiveHandoff } from './handoff.js';
import { JudgeSettings } from './JudgeSettings.jsx';
import './style.css';

// A modeling page on another origin hands over its login after opening this window.
async function acceptHandoff() {
  if (!window.opener) return;
  const response = await fetch('/evaluation/api/client-config').catch(() => null);
  if (!response?.ok) return;
  const { trusted_origins: trusted = [] } = await response.json();
  const tokens = await receiveHandoff({ opener: window.opener, trusted, here: location.origin, target: window });
  if (tokens) { clearLogin(); saveLogin(tokens); }
}

function App() {
  const query = new URLSearchParams(location.search);
  const [session, setSession] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [packages, setPackages] = useState([]), [versions, setVersions] = useState([]);
  const [packageId, setPackageId] = useState(query.get('hub_package_id') || ''), [releaseId, setReleaseId] = useState(query.get('release_id') || '');
  const [sets, setSets] = useState([]), [sources, setSources] = useState([]), [runs, setRuns] = useState([]);
  const [setId, setSetId] = useState(''), [sourceId, setSourceId] = useState(''), [prep, setPrep] = useState(null), [detail, setDetail] = useState(null), [results, setResults] = useState([]);
  const [setName, setSetName] = useState(''), [caseText, setCaseText] = useState('');
  const [sourceName, setSourceName] = useState(''), [mode, setMode] = useState('none'), [provider, setProvider] = useState('nl2sql_http');
  const [connection, setConnection] = useState('{}'), [schema, setSchema] = useState('{}'), [inputIds, setInputIds] = useState([]), [importData, setImportData] = useState(false);
  const [receipt, setReceipt] = useState(null), [answer, setAnswer] = useState(null), [nextRuns, setNextRuns] = useState(null), [nextPackages, setNextPackages] = useState(null), [nextVersions, setNextVersions] = useState(null), [nextResults, setNextResults] = useState(null);
  const [trace, setTrace] = useState(null);
  const [setEdit, setSetEdit] = useState(null), [sourceEdit, setSourceEdit] = useState(null), [judgeOpen, setJudgeOpen] = useState(false);
  async function allDefinitions(path) { let items = [], offset = 0; do { const page = await api(`${path}?offset=${offset}`); items.push(...page.items); offset = page.next_offset; } while (offset != null); return { items }; }
  async function act(work) { setBusy(true); setError(''); try { await work(); } catch (e) { setError(e.message); } finally { setBusy(false); } }
  async function refresh() {
    const [p, s, d, r] = await Promise.all([api('/packages'), allDefinitions('/eval-sets'), allDefinitions('/data-sources'), api('/runs')]);
    setPackages(p.items); setNextPackages(p.next_offset); setSets(s.items); setSources(d.items); setRuns(r.items); setNextRuns(r.next_offset);
  }
  function reset() { setSession(null); setPrep(null); setDetail(null); setResults([]); setAnswer(null); setTrace(null); setReceipt(null); setPackages([]); setVersions([]); setRuns([]); setSets([]); setSources([]); setInputIds([]); setConnection('{}'); setSchema('{}'); setCaseText(''); setSetName(''); setSourceName(''); setSetId(''); setSourceId(''); setSetEdit(null); setSourceEdit(null); setJudgeOpen(false); sessionStorage.removeItem('evaluation-subject'); }
  useEffect(() => {
    act(async () => { await acceptHandoff(); setSession(await api('/session')); await refresh(); });
    const changed = (e) => { if (e.type !== 'storage' || e.key == null || [TOKEN, REFRESH].includes(e.key)) { reset(); act(async () => { setSession(await api('/session')); await refresh(); }); } };
    const required = () => reset();
    window.addEventListener('storage', changed); window.addEventListener('evaluation-auth-required', required);
    return () => { window.removeEventListener('storage', changed); window.removeEventListener('evaluation-auth-required', required); };
  }, []);
  useEffect(() => {
    if (session?.subject) sessionStorage.setItem('evaluation-subject', session.subject);
    setPrep(null);
    if (!session || !packageId) return;
    act(async () => {
      const page = await api(`/packages/${encodeURIComponent(packageId)}/releases`);
      if (releaseId && !page.items.some((r) => r.release_id === releaseId)) {
        const selected = await api(`/packages/${encodeURIComponent(packageId)}/releases/${encodeURIComponent(releaseId)}`);
        page.items.unshift(selected);
      }
      const selected = page.items.find((r) => r.release_id === releaseId);
      if (query.get('release_id') === releaseId && query.get('package_digest') && selected?.package_digest !== query.get('package_digest')) throw new Error('跳转链接的包摘要与 Hub 不一致');
      setVersions(page.items); setNextVersions(page.next_offset);
    });
  }, [packageId, session?.subject]);
  useEffect(() => { setPrep(null); }, [releaseId, sourceId]);
  useEffect(() => {
    if (!session) return;
    const timer = setInterval(() => { api('/runs').then((r) => { setRuns(r.items); setNextRuns(r.next_offset); }).catch((e) => setError(e.message)); }, 5000);
    return () => clearInterval(timer);
  }, [session?.subject]);
  async function showRun(id) {
    const [r, cases] = await Promise.all([api(`/runs/${id}`), api(`/runs/${id}/results`)]);
    setDetail(r); setResults(cases.items); setNextResults(cases.next_offset); setAnswer(null);
  }
  const release = versions.find((v) => v.release_id === releaseId);
  return <main><header><div><small>KNOWLEDGE EVALUATION</small><h1>知识包评测</h1></div>{session && <div><button onClick={() => setJudgeOpen(!judgeOpen)}>评测判分模型配置</button><button onClick={() => { clearLogin(); reset(); }}>退出登录</button></div>}</header>
    {error && <p role="alert" className="error">{error}</p>}
    {session && judgeOpen && <JudgeSettings onClose={() => setJudgeOpen(false)} onChanged={() => act(async () => setSession(await api('/session')))} />}
    {!session ? <section><h2>登录统一账号</h2><p>从建模页面进入时将自动复用有效登录。登录过期后请重新认证。</p><form onSubmit={(e) => { e.preventDefault(); const f = new FormData(e.currentTarget); act(async () => { saveLogin(await api('/auth/login', { username: f.get('username'), password: f.get('password') })); setSession(await api('/session')); await refresh(); }); }}><input name="username" aria-label="账号" autoComplete="username" required /><input name="password" type="password" aria-label="密码" autoComplete="current-password" required /><button disabled={busy}>登录</button></form></section> : <>
    <p className="notice">问答模型使用 consumer 当前配置；Judge：{session.judge.model || '未配置'}。临时配置方案，待统一整改（MODEL-CONFIG-01）。</p>
    <section><h2>1 · 选择 Hub 版本</h2><div className="row"><select aria-label="知识包" value={packageId} onChange={(e) => { setPackageId(e.target.value); setReleaseId(''); }}><option value="">选择知识包</option>{packages.map((p) => <option key={p.hub_package_id} value={p.hub_package_id}>{p.manifest_id}</option>)}</select><select aria-label="版本" value={releaseId} onChange={(e) => setReleaseId(e.target.value)}><option value="">选择版本</option>{versions.map((r) => <option key={r.release_id} value={r.release_id}>{r.version} · {r.source_service}</option>)}</select></div>
    {nextPackages != null && <button onClick={() => act(async () => { const p = await api(`/packages?offset=${nextPackages}`); setPackages([...packages, ...p.items]); setNextPackages(p.next_offset); })}>更多知识包</button>}
    {nextVersions != null && <button onClick={() => act(async () => { const p = await api(`/packages/${packageId}/releases?offset=${nextVersions}`); setVersions([...versions, ...p.items]); setNextVersions(p.next_offset); })}>更多版本</button>}
    {release && <p>版本 {release.version} · 摘要 <code>{release.package_digest}</code></p>}</section>
    <div className="grid"><section><h2>2 · 评测集</h2><select aria-label="评测集" value={setId} onChange={(e) => setSetId(e.target.value)}><option value="">选择评测集</option>{sets.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select>
      <button onClick={() => act(async () => { const item = await api(`/eval-sets/${setId}`); setSetEdit(item); setSetName(item.name); setCaseText(JSON.stringify(item.cases.map(({ question, reference }) => ({ question, reference })), null, 2)); })} disabled={!setId}>编辑所选评测集</button><details open={!!setEdit}><summary>{setEdit ? "编辑评测集" : "新建评测集"}</summary><input placeholder="评测集名称" value={setName} onChange={(e) => setSetName(e.target.value)} /><p>JSON 数组，每项包含 question 和可选 reference；未填写参考答案将等待人工判定。</p><textarea aria-label="评测用例 JSON" value={caseText} onChange={(e) => setCaseText(e.target.value)} /><button disabled={busy || !setName} onClick={() => act(async () => { const value = await api(setEdit ? `/eval-sets/${setEdit.id}` : '/eval-sets', { name: setName, cases: JSON.parse(caseText), ...(setEdit ? { revision: setEdit.revision } : {}) }, setEdit ? 'PUT' : 'POST'); setSetId(value.id); setSetEdit(null); await refresh(); })}>保存评测集</button></details>
      {setId && <button onClick={() => act(async () => { await api(`/eval-sets/${setId}`, undefined, 'DELETE'); setSetId(''); await refresh(); })}>删除评测集定义</button>}
    </section><section><h2>3 · 数据源</h2><select aria-label="数据源" value={sourceId} onChange={(e) => setSourceId(e.target.value)}><option value="">无数据源（none）</option>{sources.map((s) => <option key={s.id} value={s.id}>{s.name} · {s.mode}</option>)}</select>
      <button disabled={!sourceId} onClick={() => act(async () => { const item = await api(`/data-sources/${sourceId}`); setSourceEdit(item); setSourceName(item.name); setMode(item.mode); setProvider(item.provider); setInputIds(item.input_ids); setSchema(JSON.stringify(item.tables)); setImportData(item.import_data); setConnection(''); })}>编辑所选数据源</button><details open={!!sourceEdit}><summary>{sourceEdit ? "编辑数据源（连接留空保留原配置）" : "新建数据源"}</summary><input placeholder="数据源名称" value={sourceName} onChange={(e) => setSourceName(e.target.value)} /><select value={mode} onChange={(e) => setMode(e.target.value)}><option>none</option><option>dataset</option><option>connection</option></select>
      {mode !== 'none' && <><select value={provider} onChange={(e) => setProvider(e.target.value)}><option>nl2sql_http</option><option>nl2sql_template</option><option>neo4j</option></select><label>CSV / XLSX 文件<input type="file" accept=".csv,.xlsx" multiple onChange={(e) => { const files = [...e.target.files]; act(async () => { const ids = [...inputIds]; for (const file of files) { const form = new FormData(); form.append('file', file); const item = await api('/inputs', form); ids.push(item.id); } setInputIds(ids); }); }} /></label><p>已上传 {inputIds.length} 个文件</p></>}
      {mode === 'connection' && <><label>连接配置 JSON（凭据加密存储）<textarea value={connection} onChange={(e) => setConnection(e.target.value)} /></label><label>表字段定义 JSON，格式为表名对应字段数组<textarea value={schema} onChange={(e) => setSchema(e.target.value)} /></label><label><input type="checkbox" checked={importData} onChange={(e) => setImportData(e.target.checked)} />明确允许将上传数据导入此连接</label></>}
      <button disabled={busy || !sourceName} onClick={() => act(async () => { const value = await api(sourceEdit ? `/data-sources/${sourceEdit.id}` : '/data-sources', { name: sourceName, mode, provider, input_ids: inputIds, tables: JSON.parse(schema), import_data: importData, ...(mode === 'connection' && connection.trim() ? { connection: JSON.parse(connection) } : {}), ...(sourceEdit ? { revision: sourceEdit.revision } : {}) }, sourceEdit ? 'PUT' : 'POST'); setConnection('{}'); setSourceEdit(null); setSourceId(value.id); setPrep(null); await refresh(); })}>保存数据源</button></details>
      {sourceId && <button onClick={() => act(async () => { await api(`/data-sources/${sourceId}`, undefined, 'DELETE'); setSourceId(''); await refresh(); })}>删除数据源定义</button>}
    </section></div>
    <section><h2>4 · 准备与执行</h2><div className="row"><button disabled={busy || !release} onClick={() => act(async () => { const input = { package_ref: { hub_package_id: packageId, release_id: releaseId, package_digest: release.package_digest }, data_source_id: sourceId }; const key = requestId('prepare', JSON.stringify(input)); const p = await api('/package-preparations', { ...input, request_id: key.id }); setPrep(p); key.done(); })}>准备知识包与映射</button>
      <button disabled={busy || !prep || !setId} onClick={() => act(async () => { const input = { preparation_id: prep.id, eval_set_id: setId }; const key = requestId('run', JSON.stringify(input)); const r = await api('/runs', { ...input, request_id: key.id }); key.done(); await refresh(); await showRun(r.id); })}>开始评测</button>
      <button disabled={busy || !prep?.mapping} onClick={() => act(async () => { const key = requestId('publish', prep.id); setReceipt(await api('/package-publications', { preparation_id: prep.id, request_id: key.id })); key.done(); })}>发布知识包版本</button></div>
      {prep && <details open><summary>映射预览 · {prep.package_ref.version}</summary><pre>{JSON.stringify(prep.preview, null, 2)}</pre></details>}{receipt && <p role="status">Hub 已发布版本 {receipt.version} · {receipt.package_digest}</p>}</section>
    <section><h2>运行记录</h2><table><thead><tr><th>轮次</th><th>状态</th><th>判分 / 总数</th><th>操作</th></tr></thead><tbody>{runs.map((r) => <tr key={r.id}><td><code>{r.id.slice(0, 12)}</code></td><td>{r.status} · {r.phase}</td><td>{r.summary.judged} / {r.summary.planned}</td><td><button onClick={() => act(() => showRun(r.id))}>详情</button><button onClick={() => act(async () => { await api(`/runs/${r.id}/cancel`, {}); await refresh(); })}>停止</button><button onClick={() => act(async () => { await api(`/runs/${r.id}/resume`, {}); await refresh(); })}>授权恢复</button></td></tr>)}</tbody></table>{nextRuns != null && <button onClick={() => act(async () => { const p = await api(`/runs?offset=${nextRuns}`); setRuns([...runs, ...p.items]); setNextRuns(p.next_offset); })}>更多记录</button>}</section>
    {detail && <section><h2>轮次详情 · {detail.status}</h2><pre>{JSON.stringify(detail.summary, null, 2)}</pre>{detail.error && <p role="alert">{detail.error.message}</p>}<table><thead><tr><th>问题</th><th>状态</th><th>判分</th><th>操作</th></tr></thead><tbody>{results.map((c) => <tr key={c.id}><td>{c.question}</td><td>{c.status}</td><td>{(c.override || c.judgement)?.verdict || '—'}</td><td><button onClick={() => act(async () => setAnswer(await api(`/runs/${detail.id}/results/${c.id}/answer`)))}>答案及证据</button></td></tr>)}</tbody></table>
    {nextResults != null && <button onClick={() => act(async () => { const p = await api(`/runs/${detail.id}/results?offset=${nextResults}`); setResults([...results, ...p.items]); setNextResults(p.next_offset); })}>更多用例</button>}
    <button onClick={() => act(async () => { await api(`/runs/${detail.id}`, undefined, 'DELETE'); setDetail(null); await refresh(); })}>删除已结束轮次及自有消费空间</button>
    {answer && <article><h3>答案</h3><pre>{answer.answer || '未获得答案'}</pre><p>证据引用：{JSON.stringify(answer.evidence || {})}</p><button onClick={() => act(async () => setTrace(await api(`/runs/${detail.id}/results/${answer.id}/evidence`)))}>获取执行事件索引</button>{trace && <pre>{JSON.stringify(trace, null, 2)}</pre>}<form onSubmit={(e) => { e.preventDefault(); const f = new FormData(e.currentTarget); act(async () => { await api(`/runs/${detail.id}/results/${answer.id}/override`, { verdict: f.get('verdict'), reason: f.get('reason'), revision: detail.revision }); await showRun(detail.id); }); }}><select name="verdict"><option>pass</option><option>partial</option><option>fail</option></select><input name="reason" placeholder="人工改判原因" required /><button disabled={busy || !answer.answer}>保存人工判定</button></form></article>}</section>}
    </>}
  </main>;
}
createRoot(document.getElementById('root')).render(<App />);
