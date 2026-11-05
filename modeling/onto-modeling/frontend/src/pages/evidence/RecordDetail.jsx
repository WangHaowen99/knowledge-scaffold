// 证据浏览器右栏：非数据库快照（程序逆向等）的记录详情（EV-25）——位置与属性、成员、关系（EV-27）、被引用（EV-26）。
// 数据库表的详情在 SchemaPage 的 Detail；「被引用」两边共用。
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { Button, Chip, Icon, KV, cx } from '../../ui/index.jsx';
import { backlinkView } from '../studio/studioModel.js';
import { parseAnchor, recordIcon, recordKindText, recordPairs } from './evidenceModel.js';
import { ConfChip, UntrustedChip, copyText } from './parts.jsx';

export function WhereUsed({ used, pid, emptyText }) {
  if (used === null) return <div className="xs muted">正在查…</div>;
  if (!used.items.length) return <div className="xs muted">{emptyText}</div>;
  return used.items.map((u, i) => {
    const v = backlinkView(u.target);
    const go = () => (v.artifact
      ? navigate(pagePath(u.target.type === 'trace.scenario' ? 'scenarios' : 'studio-table', pid), u.target.key ? { key: u.target.key } : undefined)
      : navigate(pagePath('studio-table', pid), { entry: u.target.id }));
    return (
      <div className="evd-wu" key={i}>
        <Icon name={v.icon} size={14} />
        <div className="grow"><b>{v.name}</b><div className="xs muted">{v.kind} · {u.location}</div></div>
        <Button size="xs" kind="ghost" icon="arrow" title="去看这条" onClick={go} />
      </div>
    );
  });
}

// 上级锚点的可读写法：路径类型后面的名字解码（orders.js::Order）
const anchorTail = (anchor) => {
  const path = parseAnchor(anchor)?.path || '';
  const i = path.indexOf(':');
  try { return decodeURIComponent(path.slice(i + 1)); } catch { return path; }
};

export function RecordDetail({ record, used, lineage, onPick, pid }) {
  const members = record.fields || [];
  const pairs = recordPairs(record).map(([k, v]) => [k, <span className={cx(k !== '来源' && 'mono')} style={{ fontSize: 12, wordBreak: 'break-all' }}>{v}</span>]);
  if (record.parent_anchor) pairs.unshift(['上级', <a className="link mono" style={{ fontSize: 12 }} onClick={() => onPick(record.parent_anchor)}>{anchorTail(record.parent_anchor)}</a>]);
  return (
    <>
      <div style={{ padding: '14px 14px 10px', borderBottom: '1px solid var(--line)' }}>
        <div className="row">
          <span style={{ width: 30, height: 30, borderRadius: 9, display: 'grid', placeItems: 'center', background: 'var(--t50)', color: 'var(--teal)', flex: 'none' }}><Icon name={recordIcon(record)} size={14} /></span>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="b lg ell">{record.name}</div>
            <div className="xs muted">{[record.label, recordKindText(record)].filter(Boolean).join(' · ')}</div>
          </div>
          {record.untrusted_content ? <UntrustedChip /> : null}
          <Chip tone="line" sm>{record.type}</Chip>
          <ConfChip k={record.confidence} />
        </div>
        <div className="row mt8">
          <span className="anchor grow ell">{record.anchor}</span>
          <Button size="xs" kind="ghost" icon="copy" title="复制锚点" onClick={async () => toast((await copyText(record.anchor)) ? `已复制锚点 ${record.anchor}` : '复制失败，请手动选择', 'copy')} />
        </div>
      </div>
      <div className="scroll evd-det" style={{ flex: 1, padding: '12px 14px 14px' }}>
        {pairs.length ? <KV w="44px" pairs={pairs} /> : null}
        {members.length ? (
          <>
            <div className="sh"><Icon name="list" size={12} />成员</div>
            {members.map((f) => (
              <div key={f.anchor} className="evd-fl" title="打开" onClick={() => onPick(f.anchor)}>
                <div><span className="mono b" style={{ fontSize: 12 }}>{f.name}</span><div className="xs muted">{f.type}</div></div>
                <div><ConfChip k={f.confidence} /></div>
              </div>
            ))}
          </>
        ) : null}
        <div className="sh"><Icon name="graph" size={12} />关系</div>
        {lineage === null ? <div className="xs muted">正在查…</div> : lineage.length ? lineage.map((l) => (
          <div className="evd-wu go" key={l.anchor} title="打开" onClick={() => onPick(l.anchor)}>
            <Icon name={recordIcon({ anchor: l.anchor })} size={14} />
            <div className="grow" style={{ minWidth: 0 }}><b className="mono ell" style={{ fontSize: 12, display: 'block' }}>{l.label}</b><div className="xs muted">{l.basis}</div></div>
            <ConfChip k={l.confidence} />
          </div>
        )) : <div className="xs muted">没有关系记录。</div>}
        <div className="sh"><Icon name="link" size={12} />被引用 <span className="faint" style={{ fontWeight: 500 }}>{used ? used.total : ''}</span></div>
        <WhereUsed used={used} pid={pid} emptyText="还没有产物引用这条记录。" />
      </div>
    </>
  );
}
