import { useState } from 'react';
import { actions } from '../lib/appStore.js';
import { impact } from '../lib/mock/fixtures.js';

/* 主视觉刻意先说「什么没变」。用户对增量最大的恐惧是被推翻，
   把「保持有效」做成绿色大数放在左边，比任何图表都有效。 */
export default function ImpactView({ data, demo = false }) {
  const [tab, setTab] = useState(0);
  const view = (() => { if (data?.intact && data?.todo) return data; return demo ? impact : null; })();
  if (!view) {
    return (
      <div className="empty" role="status">
        影响数据暂不可用，请稍后重试。现有知识内容不会因此被自动判定为保持有效。
      </div>
    );
  }
  if (view.scopeResolved === false) {
    return (
      <section className="wb-impact-pending" role="status" aria-live="polite">
        <span className="sp" aria-hidden="true" />
        <div>
          <strong>正在界定影响范围</strong>
          <p>新资料还没有对应的历史来源关系。助手正在核对资料与现有知识，当前不会把任何内容判为保持有效或需要修改。</p>
          <span>{view.todo.n} 项现有业务内容待核对</span>
        </div>
      </section>
    );
  }
  return (
    <>
      <div className="wb-split">
        <div className="wb-stat good">
          <div className="n">{view.intact.n}</div>
          <div className="lb">项保持有效，不会被改动</div>
          <ul>{(view.intact.items || []).map((t) => <li key={t}>{t}</li>)}</ul>
        </div>
        <div className="wb-stat todo">
          <div className="n">{view.todo.n}</div>
          <div className="lb">项需要处理（其中 {view.todo.decide || 0} 项要你裁决）</div>
          <ol>{(view.todo.items || []).map((it) => (
            <li key={it.t}><b>{it.t}</b> · {it.s}</li>
          ))}</ol>
        </div>
      </div>

      <div className="tabs" style={{ marginBottom: 'var(--s-5)' }} role="tablist">
        {['证据链', '清单'].map((t, i) => (
          <button key={t} role="tab" aria-selected={tab === i}
            className={tab === i ? 'on' : ''} onClick={() => setTab(i)}>{t}</button>
        ))}
      </div>

      {tab === 0 ? (
        <>
          <div className="wb-chain">
            {(view.chains || []).map((c, i) => (
              <Chain key={i} c={c} />
            ))}
          </div>
          <p className="t-sm muted" style={{ marginTop: 'var(--s-4)' }}>
            点任一条链可以左右展开：左边看资料原文，右边看知识条目当前内容。
          </p>
        </>
      ) : (
        <table className="grid">
          <thead><tr><th style={{ width: 190 }}>受影响内容</th><th style={{ width: 110 }}>失效原因</th><th>新资料证据</th></tr></thead>
          <tbody>
            {(view.chains || []).map((c, i) => (
              <tr key={i}>
                <td><code className="k">{c.dst}</code></td>
                <td className="t-sm muted">{c.dstSub}</td>
                <td className="t-sm">{c.src}<div className="faint t-xs">{c.srcSub}</div></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Chain({ c }) {
  return (
    <>
      <button type="button" className="cell src" disabled={!c.srcDocumentId}
        onClick={() => actions.selectMaterial(c.srcDocumentId)}
        aria-label={`查看资料原文：${c.src}`}>
        <b>{c.src}</b><span>{c.srcSub}</span>
      </button>
      <div className="arr">→</div>
      <button type="button" className="cell dst" disabled={!c.dstPath}
        onClick={() => actions.selectPath(c.dstPath)}
        aria-label={`查看知识内容：${c.dst}`}>
        <b>{c.dst}</b><span>{c.dstSub}</span>
      </button>
    </>
  );
}
