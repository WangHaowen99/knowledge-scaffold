import { gates } from '../lib/mock/fixtures.js';
import { actions } from '../lib/appStore.js';

const MARK = { pass: '✓', fail: '✕', na: '–' };

/* 门禁是纯谓词，每次打开实时重算。每条阻断项都带可执行出口，不是只给一个红叉。 */
export default function GateView({ data }) {
  const items = Array.isArray(data?.gates) ? data.gates : gates;
  return (
    <>
      {items.map((g) => (
        <div className="wb-gate" key={g.id}>
          <div className={`ic ${g.st}`} aria-hidden="true">{MARK[g.st]}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="gt">{g.t}</div>
            <div className="gd">{g.d}</div>
            {g.blockers && <ul>{g.blockers.map((b) => <li key={b}>{b}</li>)}</ul>}
            {g.actions && (
              <div className="btn-row" style={{ marginTop: 'var(--s-3)' }}>
                {g.actions.map((a, i) => {
                  const label = typeof a === 'string' ? a : a.label;
                  const message = typeof a === 'string' ? '' : a.message;
                  const className = `btn btn-sm${(typeof a === 'object' ? a.primary : i === 1) ? ' btn-p' : ''}`;
                  return (
                  <button key={label} className={className}
                    onClick={() => {
                      if (isEvaluationAction(a)) actions.openEvaluation();
                      else if (message) void actions.send(message);
                    }}>{label}</button>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      ))}
      <p className="t-sm muted" style={{ marginTop: 'var(--s-5)' }}>
        v1 没有静态校验器，门禁全部建立在真实执行结果上：沙箱测试跑过、评测跑过、候选被确认过。
        「用例覆盖已确认」因此成为最重要的一道人工闸门。
      </p>
    </>
  );
}

export function isEvaluationAction(action) {
  return Boolean(action && typeof action === 'object'
    && (action.route === 'evaluation' || action.label === '前往评测界面'));
}
