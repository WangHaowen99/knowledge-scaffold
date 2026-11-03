// 全局页的二级导航（设计稿 core.js 的 snav、pages-60 的 plSecNav）：分组标题 + 链接项。
// 项带 go 时跳到那一页（可带 query），否则调 onPick(key) 在本页切换分区。
import { navigate, pagePath } from '../app/router.js';
import { Icon } from '../ui/index.jsx';

/**
 * @param {object} o
 * @param {{title: string, items: {key: string, title: string, icon: string, extra?: any, go?: string, query?: object, hidden?: boolean}[]}[]} o.groups
 * @param {string} o.active 当前项的 key
 * @param {(key: string) => void} [o.onPick]
 * @param {any} [o.top] 分组之前的内容（运维中心的身份卡）
 * @param {any} [o.bottom] 分组之后的内容（说明、监听地址）
 */
export function SNav({ groups, active, onPick, top, bottom }) {
  const pick = (it) => {
    if (it.go) navigate(pagePath(it.go), it.query);
    else onPick?.(it.key);
  };
  return (
    <aside className="snav">
      {top}
      {groups.map((g) => {
        const items = g.items.filter((it) => !it.hidden);
        if (!items.length) return null;
        return [
          g.title ? <h4 key={`h-${g.title}`}>{g.title}</h4> : null,
          ...items.map((it) => (
            <a key={it.key} className={it.key === active ? 'on' : ''} onClick={() => pick(it)} role="link" tabIndex={0}
              onKeyDown={(e) => { if (e.key === 'Enter') pick(it); }}>
              <Icon name={it.icon} size={14} />
              <span className="sp">{it.title}</span>
              {it.extra ?? null}
            </a>
          )),
        ];
      })}
      {bottom}
    </aside>
  );
}
