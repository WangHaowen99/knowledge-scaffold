import { useEffect, useState } from 'react';
import {
  listRounds, proposeOptimize, listSuggestions, applyOptimize, rejectSuggestions,
  listVersions, exportVersionUrl, getToken,
} from './api.js';

/* ================= 节点 10 · 优化建议与应用 ================= */

const KIND_LABEL = {
  add_operation: '新增操作', modify_operation: '修改操作',
  add_object: '新增对象', modify_object: '修改对象', clarify_description: '澄清描述',
};

export function OptimizePanel({ projectId, onGoEvaluate }) {
  const [rounds, setRounds] = useState(null);
  const [versions, setVersions] = useState([]);
  const [suggestions, setSuggestions] = useState([]);
  const [sel, setSel] = useState(new Set());
  const [busy, setBusy] = useState('');   // '' | propose | apply
  const [err, setErr] = useState('');
  const [applied, setApplied] = useState(null); // { version, applied }

  const load = () => Promise.all([
    listRounds(projectId).then((d) => { setRounds(d.rounds); return d.rounds; }),
    listVersions(projectId).then((d) => setVersions(d.versions)),
    listSuggestions(projectId).then((d) => {
      setSuggestions(d.suggestions);
      setSel(new Set(d.suggestions.filter((s) => s.status === 'pending').map((s) => s.id)));
    }),
  ]).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [projectId]);

  if (!rounds) return <div className="empty">{err || '加载中…'}</div>;
  const latestDone = rounds.find((r) => r.status === 'done');
  const pending = suggestions.filter((s) => s.status === 'pending');

  const propose = async () => {
    setBusy('propose'); setErr(''); setApplied(null);
    try {
      const d = await proposeOptimize(projectId, latestDone?.id);
      setSuggestions((prev) => {
        const known = new Set(prev.map((s) => s.id));
        return [...prev, ...d.suggestions.filter((s) => !known.has(s.id))];
      });
      setSel(new Set(d.suggestions.filter((s) => s.status === 'pending').map((s) => s.id)));
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const apply = async () => {
    setBusy('apply'); setErr('');
    try {
      const d = await applyOptimize(projectId, [...sel]);
      setApplied(d);
      await load();
    } catch (e) { setErr(e.message); } finally { setBusy(''); }
  };

  const reject = async (id) => {
    try {
      const d = await rejectSuggestions(projectId, [id]);
      setSuggestions(d.suggestions);
      setSel((prev) => { const n = new Set(prev); n.delete(id); return n; });
    } catch (e) { setErr(e.message); }
  };

  const toggle = (id) => setSel((prev) => {
    const n = new Set(prev);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  return (
    <div>
      {err && <div className="lerr">{err}</div>}

      {/* 生成建议 */}
      <div className="ev-launch">
        <div style={{ fontSize: 13, color: 'var(--ink2)', lineHeight: 1.7, marginBottom: 10 }}>
          优化器分析<b>最近一轮评测的未通过用例</b>，产出对知识包规格的结构化建议。
          勾选采纳后一键应用：生成新版本 → 回到评测运行验证提升。
        </div>
        {latestDone
          ? <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>
              基于轮次：包 v{latestDone.packageVersion} · 通过 {latestDone.passCount}/{latestDone.progressTotal}
            </div>
          : <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 10 }}>暂无已完成的评测轮次，请先运行评测</div>}
        <button className="btn btn-p" disabled={!!busy || !latestDone || latestDone.passCount === latestDone.progressTotal}
          onClick={propose}>
          {busy === 'propose' ? '优化器分析中…' : '⚙ 生成优化建议'}
        </button>
        {latestDone && latestDone.passCount === latestDone.progressTotal && (
          <span style={{ marginLeft: 10, fontSize: 12.5, color: 'var(--muted)' }}>最近一轮已全部通过，无需优化</span>
        )}
      </div>

      {/* 建议列表 */}
      {suggestions.length > 0 && (
        <div style={{ marginTop: 16 }}>
          {suggestions.map((s) => (
            <div key={s.id} className={`sg-row ${s.status !== 'pending' ? 'settled' : ''}`}>
              <label className="sg-main">
                {s.status === 'pending'
                  ? <input type="checkbox" checked={sel.has(s.id)} onChange={() => toggle(s.id)} />
                  : <span className={`tag ${s.status === 'applied' ? 'tag-ok' : 'tag-lang'}`}>
                      {s.status === 'applied' ? `已应用 ${s.appliedVersion}` : '已忽略'}
                    </span>}
                <div>
                  <div className="sg-title">
                    <span className="tag tag-lang">{KIND_LABEL[s.kind] ?? s.kind}</span> {s.title}
                  </div>
                  <div className="sg-detail">{s.detail}</div>
                  {s.relatedQuestion && <div className="sg-q">关联用例：{s.relatedQuestion}</div>}
                </div>
              </label>
              {s.status === 'pending' && (
                <button className="tc-del" title="忽略该建议" onClick={() => reject(s.id)}>✕</button>
              )}
            </div>
          ))}
          {pending.length > 0 && (
            <button className="btn btn-p" style={{ marginTop: 8 }} disabled={!!busy || !sel.size} onClick={apply}>
              {busy === 'apply' ? '应用中…（生成新版本）' : `✔ 应用选中的 ${sel.size} 条建议`}
            </button>
          )}
        </div>
      )}

      {applied && (
        <div className="sg-applied">
          ✓ 已生成新版本 <b>v{applied.version}</b>（应用 {applied.applied} 条建议）。
          <button className="btn btn-p" style={{ marginLeft: 12, padding: '4px 14px', fontSize: 12.5 }} onClick={onGoEvaluate}>
            去评测验证 →
          </button>
        </div>
      )}

      {/* 版本历史与交付 */}
      {versions.length > 0 && (
        <div style={{ marginTop: 22 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>版本历史与交付</div>
          <VersionTable projectId={projectId} versions={versions} />
        </div>
      )}
    </div>
  );
}

export function VersionTable({ projectId, versions }) {
  const download = async (version) => {
    const res = await fetch(exportVersionUrl(projectId, version), { headers: { Authorization: `Bearer ${getToken()}` } });
    if (!res.ok) return;
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1]) || `package-${version}.zip`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <table className="dt">
      <thead><tr><th>版本</th><th>说明</th><th>评测通过率</th><th></th></tr></thead>
      <tbody>
        {versions.map((v, i) => {
          // v.rounds 按创建时间倒序，[0] 即该版本最新一轮
          const done = v.rounds.filter((r) => r.status === 'done');
          const best = done.length ? done[0] : null;
          const prev = versions[i - 1];
          const prevDone = prev ? prev.rounds.filter((r) => r.status === 'done') : [];
          const prevBest = prevDone.length ? prevDone[0] : null;
          const improved = best && prevBest && best.progressTotal === prevBest.progressTotal && best.passCount > prevBest.passCount;
          return (
            <tr key={v.id}>
              <td style={{ fontFamily: 'ui-monospace, monospace', fontWeight: 650 }}>v{v.version}</td>
              <td style={{ color: 'var(--ink2)' }}>{v.note || '初始生成'}</td>
              <td>
                {best
                  ? <span>{best.passCount}/{best.progressTotal}{improved ? <span className="sg-up"> ↑提升</span> : ''}</span>
                  : <span style={{ color: 'var(--muted)' }}>未评测</span>}
              </td>
              <td><button className="btn" style={{ padding: '3px 10px', fontSize: 11.5 }} onClick={() => download(v.version)}>导出 zip</button></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
