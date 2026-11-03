// Studio 填证据：先选资料、再选位置（R-01 → R-15）。位置只来自文本（ADR-0076），选中就把锚点写进 evidence_anchor，
// 写法和助手写的一样。菜单挂到 body、按单元格位置固定定位，不被属性表的圆角容器裁掉；放不下时往上开
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useApp, useProject } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { Chip, Icon, Inp, cx, useEscape } from '../../ui/index.jsx';
import { evidenceMaterials, evidencePickLabel, positionGroups } from './studioModel.js';

const MENU_W = 340;
const MENU_H = 360;
const SEARCH_MS = 250;

function placeAt(el) {
  const r = el.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.right - MENU_W, window.innerWidth - MENU_W - 8));
  return window.innerHeight - r.bottom >= MENU_H + 12 ? { left, top: r.bottom + 4 } : { left, bottom: window.innerHeight - r.top + 4 };
}

function Materials({ onOpen, onExpert }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [groups, setGroups] = useState(null);
  const [error, setError] = useState(null);
  const [q, setQ] = useState('');
  useEffect(() => {
    let live = true;
    api.resources(pid).then((r) => live && setGroups(r.groups || [])).catch((e) => live && setError(e));
    return () => { live = false; };
  }, [api, pid]);
  const rows = groups ? evidenceMaterials(groups, q) : [];
  return (
    <>
      <Inp icon="search" autoFocus placeholder="搜索资料" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="lst">
        {error ? <div className="tip">{errorText(error)}</div> : null}
        {!groups && !error ? <div className="tip">正在读取…</div> : null}
        {groups && !rows.length ? <div className="tip">{q ? '没有匹配的资料' : '没有可引用的资料'}</div> : null}
        {rows.map((m) => (
          <div key={m.path} className={cx('mi', !m.ready && 'off')} onClick={m.ready ? () => onOpen(m) : undefined}>
            <Icon name="filetext" size={14} /><span className="ell grow">{m.title}</span>
            {m.ready ? <span className="k">{m.type}</span> : <Chip tone="blue" sm>转换中</Chip>}
          </div>
        ))}
      </div>
      <hr />
      <div className="mi" onClick={onExpert}><Icon name="user" size={14} /><span>标为专家确认</span></div>
    </>
  );
}

function Positions({ material, onBack, onPick }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [q, setQ] = useState('');
  const [page, setPage] = useState(null);
  const [error, setError] = useState(null);
  const [more, setMore] = useState(false);
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      setError(null);
      api.resourcePositions(pid, material.path, { q: q.trim() })
        .then((r) => live && setPage(r)).catch((e) => live && setError(e));
    }, q ? SEARCH_MS : 0);
    return () => { live = false; clearTimeout(t); };
  }, [api, pid, material.path, q]);
  const loadMore = async () => {
    setMore(true);
    try {
      const r = await api.resourcePositions(pid, material.path, { q: q.trim(), cursor: page.next_cursor });
      setPage((p) => ({ ...r, items: [...p.items, ...r.items] }));
    } catch (e) {
      setError(e);
    } finally {
      setMore(false);
    }
  };
  const name = page?.name || material.title;
  return (
    <>
      <div className="hd">
        <button type="button" className="bk" title="换一份资料" onClick={onBack}><Icon name="arrowl" size={14} /></button>
        <span className="ell grow">{material.title}</span>
      </div>
      <Inp icon="search" autoFocus placeholder="搜索原文" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="lst">
        {error ? <div className="tip">{errorText(error)}</div> : null}
        {!page && !error ? <div className="tip">正在读取…</div> : null}
        {page && !page.items.length ? <div className="tip">{q ? '没有匹配的内容' : '没有可引用的位置'}</div> : null}
        {page ? positionGroups(page.items).map((g, gi) => (
          <div key={`${g.location}-${gi}`}>
            {g.location ? <div className="loc">{g.location}</div> : null}
            {g.items.map((it) => (
              <div key={it.anchor} className="mi" title={it.text} onClick={() => onPick(it.anchor, evidencePickLabel(name, it.location))}>
                <span className="ell grow">{it.text}</span>
              </div>
            ))}
          </div>
        )) : null}
        {page?.next_cursor ? <div className="mi more" onClick={more ? undefined : loadMore}>{more ? '正在读取…' : '更多'}</div> : null}
      </div>
    </>
  );
}

export default function EvidencePicker({ anchorRef, onPick, onExpert, onClose }) {
  const [at, setAt] = useState(() => (anchorRef.current ? placeAt(anchorRef.current) : null));
  const [material, setMaterial] = useState(null);
  const ref = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEscape(true, onClose);
  useEffect(() => {
    const down = (e) => {
      if (ref.current?.contains(e.target) || anchorRef.current?.contains(e.target)) return;
      closeRef.current();
    };
    // 页面滚动时跟着单元格走；菜单里自己的列表滚动不算
    const move = (e) => {
      if (ref.current?.contains(e.target)) return;
      if (anchorRef.current) setAt(placeAt(anchorRef.current));
    };
    document.addEventListener('mousedown', down);
    document.addEventListener('scroll', move, true);
    window.addEventListener('resize', move);
    return () => {
      document.removeEventListener('mousedown', down);
      document.removeEventListener('scroll', move, true);
      window.removeEventListener('resize', move);
    };
  }, [anchorRef]);
  if (!at) return null;
  return createPortal(
    <div ref={ref} className="menu stt-evp" style={at}>
      {material
        ? <Positions material={material} onBack={() => setMaterial(null)} onPick={onPick} />
        : <Materials onOpen={setMaterial} onExpert={onExpert} />}
    </div>,
    document.body,
  );
}
