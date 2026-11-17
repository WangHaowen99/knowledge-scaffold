// 插件中心的卡片、推荐带、界面标记（设计稿 plCard、plFoot、plUi、.pl-rec）
import { navigate, pagePath } from '../../app/router.js';
import { Button, Chip, Icon, cx } from '../../ui/index.jsx';
import {
  PACK_CATEGORIES, PACK_ICON_CLASS, cardFoot, cardTags, isInstalled, recView, uiChip, versionOf,
} from './catalogModel.js';

export function PackIcon({ category }) {
  return <span className={cx('pl-ico', PACK_ICON_CLASS[category] || 'exp')}><Icon name={PACK_CATEGORIES[category]?.[1] || 'puzzle'} size={18} /></span>;
}

// 界面标记；纯数据包没有 ui，弹窗里按「无独立页面」显示
export function UiChip({ ui, sm = true, fallback }) {
  const c = uiChip(ui) || (fallback ? { text: '无独立页面', tone: 'gray' } : null);
  if (!c) return null;
  return <Chip tone={c.tone} sm={sm} icon={c.icon || undefined} data-pop={c.pop || undefined}>{c.text}</Chip>;
}

export const goDetail = (name, projectId) => navigate(pagePath('plugin-detail'), projectId ? { pack: name, project: projectId } : { pack: name });

function FootPart({ part, busy, onAction, detail }) {
  if (!part) return null;
  if (part.type === 'chip') return <Chip tone={part.tone} sm icon={part.icon || undefined}>{part.text}</Chip>;
  if (part.type === 'detail') return <Button size="xs" kind="ghost" onClick={detail}>详情</Button>;
  return (
    <Button size="xs" kind={part.kind} icon={part.icon || undefined} disabled={part.disabled || busy} title={part.title || undefined} onClick={() => onAction?.(part.action)}>
      {part.label}
    </Button>
  );
}

/** 一张插件卡。pendingKind 来自 PL-05；busy 是这张卡正在做兼容检查 */
export function PackCard({ p, projectId, pendingKind, busy, onAction }) {
  const { tags } = cardTags(p);
  const foot = cardFoot(p, { pendingKind });
  const detail = () => goDetail(p.name, projectId);
  return (
    <div className={cx('card pl-card', isInstalled(p) && 'on')}>
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <PackIcon category={p.category} />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row g4"><span className="pl-name ell">{p.label}</span>{p.badges?.includes('new') ? <span className="badge-new">新</span> : null}</div>
          <div className="mono xs muted ell">{p.name}@{versionOf(p)}</div>
        </div>
      </div>
      <div className="pl-desc">{p.description}</div>
      <div className="row wrap g4">
        {tags.map((t) => <Chip key={t.text} tone={t.tone} sm icon={t.icon || undefined} data-pop={t.pop || undefined}>{t.text}</Chip>)}
      </div>
      <div className="pl-foot">
        <FootPart part={foot.left} busy={busy} onAction={(a) => onAction(a, p)} detail={detail} />
        {foot.note ? <span className={foot.noteClass}>{foot.note}</span> : <span className="sp" />}
        <FootPart part={foot.right} busy={busy} onAction={(a) => onAction(a, p)} detail={detail} />
      </div>
    </div>
  );
}

/** 为你的项目推荐（PL-01 recommendations；空时整条不出现，由页面判断） */
export function RecBar({ recs, items, busy, onAction }) {
  return (
    <div className="pl-rec mt16">
      <div style={{ width: 150, flex: 'none' }}>
        <div className="row g4 b" style={{ color: 'var(--blue2)' }}><Icon name="sparkle" size={14} />为你的项目推荐</div>
      </div>
      {recs.map((r) => {
        const item = items.find((x) => x.name === r.pack.name);
        const v = recView(r, item);
        return (
          <div className="it" key={r.pack.name}>
            <PackIcon category={item?.category || 'capability'} />
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="b sm">{r.pack.label} {r.pack.version}</div>
              <div className="xs muted">{r.reason}</div>
            </div>
            {v.done ? <Chip tone="blue" icon="check">{v.done}</Chip> : (
              <Button size="sm" kind="soft" disabled={!item || busy === r.pack.name} onClick={() => item && onAction(r.action === 'enable' ? 'enable' : 'update', item)}>{v.label}</Button>
            )}
          </div>
        );
      })}
    </div>
  );
}
