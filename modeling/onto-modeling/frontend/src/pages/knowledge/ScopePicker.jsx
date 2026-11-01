// 知识条目的「适用范围」选择：全局，或某个本体条目（ST-03 搜索）。知识沉淀页的新增 / 编辑弹窗、对话里「沉淀为业务知识」共用。
import { useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { Icon, cx, useOutside } from '../../ui/index.jsx';
import { KIND_LABEL, scopeLabel } from './knowledgeModel.js';
import '../../styles/knowledge.css';

const KINDS = 'object,operation,rule,metric';
const MENU_H = 300;

// 弹窗正文会滚动、会裁掉溢出的内容：菜单按触发框的位置固定定位，下方放不下就往上开
function menuPos(el) {
  const r = el.getBoundingClientRect();
  const up = window.innerHeight - r.bottom < MENU_H && r.top > MENU_H;
  return up ? { position: 'fixed', left: r.left, width: r.width, bottom: window.innerHeight - r.top + 4 } : { position: 'fixed', left: r.left, width: r.width, top: r.bottom + 4 };
}

export function ScopePicker({ value, onChange, disabled }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [items, setItems] = useState(null);
  const [pos, setPos] = useState(null);
  const ref = useRef(null);
  const trigger = useRef(null);
  const menu = useRef(null);
  useOutside(ref, () => setOpen(false), open);

  // 外面滚动了就收起（菜单是固定定位，不跟着走）
  useEffect(() => {
    if (!open) return undefined;
    const h = (e) => { if (!menu.current?.contains(e.target)) setOpen(false); };
    window.addEventListener('scroll', h, true);
    return () => window.removeEventListener('scroll', h, true);
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    let alive = true;
    const t = setTimeout(() => {
      api.studioEntries(pid, { kind: KINDS, q: q.trim() || undefined, limit: 50 })
        .then((r) => { if (alive) setItems(r.items || []); })
        .catch(() => { if (alive) setItems([]); });
    }, q ? 180 : 0);
    return () => { alive = false; clearTimeout(t); };
  }, [api, pid, open, q]);

  const pick = (scope) => { onChange?.(scope); setOpen(false); setQ(''); };
  const current = value?.kind === 'entries' ? (value.entries || []).map((e) => e.id) : [];
  return (
    <div ref={ref} className="kn-scope">
      <div
        ref={trigger}
        className={cx('inp sel', disabled && 'kn-dis')}
        role="button"
        onClick={() => {
          if (disabled) return;
          if (!open) setPos(menuPos(trigger.current));
          setOpen((v) => !v);
        }}
      >
        <Icon name="target" size={14} />
        <span className="grow ell">{scopeLabel(value)}</span>
      </div>
      {open ? (
        <div ref={menu} className="menu kn-scope-menu" style={pos || undefined}>
          <div className="inp kn-scope-q"><Icon name="search" size={14} /><input autoFocus placeholder="搜索对象、操作、规则" value={q} onChange={(e) => setQ(e.target.value)} /></div>
          <div className="kn-scope-list">
            <div className="mi" onClick={() => pick({ kind: 'global' })}>
              <Icon name="globe" size={14} /><span>全局（每次抽取都加载）</span>
              {value?.kind !== 'entries' ? <span className="k"><Icon name="check" size={12} /></span> : null}
            </div>
            {(items || []).map((e) => (
              <div key={e.id} className="mi" onClick={() => pick({ kind: 'entries', entries: [{ kind: e.kind, id: e.id, key: e.key, name: e.name }] })}>
                <span className="kn-scope-k">{KIND_LABEL[e.kind] || e.kind}</span>
                <span className="ell">{e.name}</span>
                {current.includes(e.id) ? <span className="k"><Icon name="check" size={12} /></span> : null}
              </div>
            ))}
            {!items ? <div className="xs muted kn-scope-none">正在读取条目…</div> : null}
            {items && !items.length ? <div className="xs muted kn-scope-none">没有匹配的条目</div> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
