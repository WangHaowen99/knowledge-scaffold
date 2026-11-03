// 悬浮说明：data-pop 显示文字；data-ev 是证据锚点，悬停时经 EV-29 解析原文（同设计稿 .pop）
import { useEffect, useRef, useState } from 'react';
import { Icon } from '../ui/index.jsx';

const CONFIDENCE = {
  declared: '系统元数据（表结构里明确写着）',
  observed: '数据观察（采样数据里看到的）',
  inferred: '推断（没有直接证据）',
};

// 由项目外壳注册：anchor → Promise<AnchorResolution>
export const anchorResolver = { current: null };

function Marked({ text, highlights }) {
  if (!highlights?.length) return text;
  const out = [];
  let pos = 0;
  highlights.forEach((h, i) => {
    if (h.start > pos) out.push(text.slice(pos, h.start));
    out.push(<mark key={i}>{text.slice(h.start, h.end)}</mark>);
    pos = h.end;
  });
  if (pos < text.length) out.push(text.slice(pos));
  return out;
}

export function PopLayer() {
  const ref = useRef(null);
  const [state, setState] = useState({ show: false, content: null, target: null });
  const cache = useRef(new Map());

  useEffect(() => {
    let current = null;
    const onOver = (e) => {
      const el = e.target.closest?.('[data-ev],[data-pop]');
      if (!el) {
        if (current) { current = null; setState((s) => ({ ...s, show: false })); }
        return;
      }
      if (el === current) return;
      current = el;
      if (el.dataset.pop) {
        setState({ show: true, content: { pop: el.dataset.pop }, target: el });
        return;
      }
      const anchor = el.dataset.ev;
      const hit = cache.current.get(anchor);
      if (hit) { setState({ show: true, content: { res: hit }, target: el }); return; }
      setState({ show: true, content: { loading: anchor }, target: el });
      anchorResolver.current?.(anchor).then((res) => {
        if (!res) return;
        cache.current.set(anchor, res);
        if (current === el) setState({ show: true, content: { res }, target: el });
      }).catch(() => {
        // 解析不了（证据源本期只留接口，EV-29 回 501）：不留一个一直转圈的浮层
        if (current === el) setState((s) => ({ ...s, show: false }));
      });
    };
    document.addEventListener('mouseover', onOver);
    return () => document.removeEventListener('mouseover', onOver);
  }, []);

  useEffect(() => {
    if (state.show && state.target) {
      const el = state.target;
      requestAnimationFrame(() => {
        const pop = ref.current;
        if (!pop) return;
        const r = el.getBoundingClientRect();
        pop.style.left = `${Math.min(r.left, window.innerWidth - 400)}px`;
        pop.style.top = `${r.bottom + 8 + 200 > window.innerHeight ? r.top - 8 - pop.offsetHeight : r.bottom + 8}px`;
      });
    }
  }, [state]);

  const c = state.content;
  let body = null;
  if (c?.pop) body = c.pop;
  else if (c?.loading) body = <div className="src"><Icon name="filetext" size={12} />正在读取出处…</div>;
  else if (c?.res) {
    const r = c.res;
    const db = r.scheme === 'db';
    body = (
      <>
        <div className="src">
          <Icon name={db ? 'db' : 'filetext'} size={12} />
          <b style={{ color: 'var(--ink2)' }}>{r.material?.name || r.source_label}</b>
          {r.location_label ? <span>· {r.location_label}</span> : null}
        </div>
        <div style={{ whiteSpace: 'pre-line' }}>
          {r.resolved ? <Marked text={r.text} highlights={r.highlights} /> : '找不到这个出处'}
        </div>
        {r.confidence && CONFIDENCE[r.confidence] && db ? <div className="mt8 xs muted">依据：{CONFIDENCE[r.confidence]}</div> : null}
      </>
    );
  }
  return <div ref={ref} className={`pop ${state.show ? 'show' : ''}`}>{body}</div>;
}
