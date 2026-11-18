// 顶栏中间的导航栏（节点模型重构 2b）：上导航栏的节点按流程顺序。点节点名展开下拉——这个节点发到对话的按钮，
// 分隔线下面是挂在它下面的可选节点（点名字开始，小按钮跳过 / 恢复），条件节点多一项「这一轮不处理」；
// 只有一个跳页面按钮的节点（发布）不做下拉，点了直接去。下拉里有什么由 flow.navMenu 算
import { useRef, useState } from 'react';
import {
  Button, Icon, St, cx, useOutside,
} from '../../ui/index.jsx';
import { navMenu, trackClass, trackNodes } from '../../state/flow.js';

const OP_LABEL = { skip: '跳过', restore: '恢复' };

function NavMenu({ node, menu, onAct }) {
  const rest = menu.optional.length > 0 || menu.endRound;
  return (
    <div className="menu wb-pop wx-nav-pop">
      {menu.buttons.map((b) => (
        <div key={`${b.label}|${b.target}`} className="mi" title={b.reason || undefined} onClick={() => onAct({ type: 'button', button: b })}>
          <span className="ell">{b.label}</span>
        </div>
      ))}
      {menu.buttons.length && rest ? <hr /> : null}
      {menu.optional.map((o) => (
        <div key={o.id} className={cx('mi wx-nav-opt', !o.start && 'off')} onClick={() => { if (o.start) onAct({ type: 'start', row: o }); }}>
          <span className="grow ell">{o.title}</span>
          <St k={o.display_state} sm />
          {o.op ? (
            <Button size="xs" kind="ghost" onClick={(e) => { e.stopPropagation(); onAct({ type: o.op, id: o.id }); }}>{OP_LABEL[o.op]}</Button>
          ) : null}
        </div>
      ))}
      {menu.endRound ? <div className="mi" onClick={() => onAct({ type: 'end_round', id: node.id })}>这一轮不处理</div> : null}
    </div>
  );
}

/**
 * pages：PL-12 的插件页（面板节点按页面声明的 data.nodes 找去处）；onOpen：展开下拉时调（重取 FL-01，动作、状态要新的）；
 * onAct({type: button|start|skip|restore|end_round, ...})：下拉里点了什么，由工作台发命令、调接口；
 * readOnly：项目已归档、平台只读维护时只留跳页面的节点（发布），别的不展开
 */
export function NavTrack({
  flow, pages, onOpen, onAct, readOnly = false,
}) {
  const nodes = trackNodes(flow);
  const [open, setOpen] = useState(null);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(null), !!open);
  const act = (a) => { setOpen(null); onAct(a); };
  return (
    <div className="wx-track" ref={ref}>
      {nodes.map((n, i) => {
        const k = trackClass(n.display_state);
        const prevOk = i > 0 && trackClass(nodes[i - 1].display_state) === 'ok';
        const full = navMenu(flow, n, pages);
        const menu = readOnly ? { ...full, empty: !full.direct } : full;
        const toggle = () => {
          if (menu.direct) act({ type: 'button', button: menu.direct });
          else if (open === n.id) setOpen(null);
          else if (!menu.empty) { setOpen(n.id); onOpen?.(); }
        };
        return (
          <span key={n.id} style={{ display: 'contents' }}>
            {i ? <span className={cx('wx-ln', prevOk && 'ok')} /> : null}
            <span className="wx-nd">
              <span className={cx('wx-st', k, !menu.empty && 'act', open === n.id && 'on')} onClick={toggle}>
                <span className="dt">{k === 'ok' ? <Icon name="check" /> : k === 'blocked' ? <Icon name="lock" /> : k === 'stale' ? <Icon name="refresh" /> : null}</span>
                {n.title.replace('（按段）', '')}
                {n.progress?.label ? <span className="x">{n.progress.label}</span> : null}
                {!menu.empty && !menu.direct ? <Icon name="chevd" size={12} className="cv" /> : null}
              </span>
              {open === n.id ? <NavMenu node={n} menu={menu} onAct={act} /> : null}
            </span>
          </span>
        );
      })}
    </div>
  );
}
