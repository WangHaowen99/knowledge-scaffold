import { useEffect, useState } from 'react';
import { conflicts } from '../lib/mock/fixtures.js';
import { actions, store } from '../lib/appStore.js';
import { useStore } from '../lib/store.js';
import FusionView from '../fusion/FusionView.jsx';

/* 选择性导入前置：勾选范围时冲突数实时反馈。
   把 8 个冲突降到 5 个的一次勾选，比裁决 8 次有价值得多。 */
const SCOPE = [
  { k: 'objects', label: 'objects/', n: 9, adds: 2 },
  { k: 'operations', label: 'operations/', n: 6, adds: 1 },
  { k: 'rules', label: 'rules/', n: 8, adds: 2 },
  { k: 'diagrams', label: 'diagrams/', n: 2, adds: 0 },
  { k: 'scripts', label: 'scripts/', n: 1, adds: 1 },
  { k: 'mappings', label: 'mappings/', n: 4, adds: 2 },
];

export default function ConflictView({ data }) {
  const demo = useStore(store, (state) => state.demo);
  const legacySingleSource = Array.isArray(data?.scope) || Array.isArray(data?.conflicts);
  if (!demo && !legacySingleSource) return <FusionView />;
  return <SingleSourceConflictView data={data} />;
}

function SingleSourceConflictView({ data }) {
  const scopes = Array.isArray(data?.scope) ? data.scope : SCOPE;
  const rows = Array.isArray(data?.conflicts) ? data.conflicts : conflicts;
  const initial = () => Object.fromEntries(scopes.map((item) => [item.k, item.selected !== false]));
  const [on, setOn] = useState(initial);
  useEffect(() => setOn(initial()), [data]);
  const total = scopes.reduce((sum, item) => sum + (on[item.k] ? Number(item.adds || 0) : 0), 0);
  const updateScope = (key) => {
    const next = { ...on, [key]: !on[key] };
    setOn(next);
    if (data) {
      const selected = scopes.filter((item) => next[item.k]).map((item) => item.label);
      void actions.send(`把融合范围调整为：${selected.join('、') || '不导入任何目录'}。请重新计算冲突并继续。`);
    }
  };

  return (
    <>
      <div className="card" style={{ marginBottom: 'var(--s-5)' }}>
        <div className="card-h">
          选择性导入 · {data?.source || '库存盘点包 0.9.0'}
          <span className="spacer" />
          <span className={`pill ${total > 3 ? 'pill-stale' : 'pill-wait'}`}>冲突 {total}</span>
        </div>
        <div className="card-b" style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--s-3) var(--s-6)' }}>
          {scopes.map((s) => (
            <label key={s.k} className="t-md" style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              <input type="checkbox" checked={!!on[s.k]} onChange={() => updateScope(s.k)} />
              <span>{s.label}</span>
              <span className="faint t-xs">{s.n} 项{s.adds ? ` · +${s.adds} 冲突` : ''}</span>
            </label>
          ))}
          <div className="t-sm muted" style={{ width: '100%' }}>
            未勾选的内容不参与融合，也不会产生冲突。先缩范围再谈冲突。
          </div>
        </div>
      </div>

      <table className="grid">
        <thead>
          <tr><th style={{ width: 130 }}>条目</th><th style={{ width: 52 }}>类型</th><th>当前包</th><th>来源包</th><th style={{ width: 132 }}>处置</th></tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.k}>
              <td><code className="k">{c.k}</code></td>
              <td className="t-sm muted">{c.kind}</td>
              <td className="t-sm">{c.mine}</td>
              <td className="t-sm">{c.theirs}</td>
              <td>
                <select className="input" style={{ padding: '3px 5px', fontSize: 'var(--t-sm)' }} defaultValue={c.options[0]}
                  onChange={(event) => { if (data) void actions.send(`冲突“${c.k}”选择“${event.target.value}”。请记录裁决并继续。`); }}>
                  {c.options.map((o) => <option key={o}>{o}</option>)}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="card" style={{ marginTop: 'var(--s-5)' }}>
        <div className="card-h">融合后引用解析（预演，未写入）</div>
        <div className="card-b t-sm">
          <div style={{ color: 'var(--c-ok)' }}>{data?.validation?.passed || '✓ key 唯一（写入安全强制）· ✓ 23 条引用可解析'}</div>
          {(data?.validation?.blocker || !data) && <div style={{ color: 'var(--c-stale)', marginTop: 2 }}>{data?.validation?.blocker || '✗ 别名冲突 1 处（object:备件 ↔ 来源 object:备件）—— 裁决后重算'}</div>}
          <div className="muted" style={{ marginTop: 6 }}>
            融合进来的每一条都会带 <code className="k">provenance: pkg:库存盘点包@0.9.0</code>，来源包升级时可再算一次增量。
          </div>
        </div>
      </div>
    </>
  );
}
