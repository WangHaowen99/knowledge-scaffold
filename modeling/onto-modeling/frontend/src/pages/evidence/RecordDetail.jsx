// 证据浏览器右栏：非数据库快照（程序逆向、代码仓库）的记录详情（EV-25）——位置与属性、代码片段、SQL、校验、成员、
// 关系（EV-27）、被引用（EV-26）。
// 数据库表的详情在 SchemaPage 的 Detail；「被引用」两边共用。
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { Button, Chip, Icon, KV, cx } from '../../ui/index.jsx';
import { backlinkView } from '../studio/studioModel.js';
import { parseAnchor, recordIcon, recordKindText, recordPairs, recordValidations, snippetLines, splitMembers } from './evidenceModel.js';
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

// 代码片段：等宽，行号从记录所在行起
function Snippet({ lines }) {
  return (
    <div className="code evp-snippet" style={{ fontSize: 11, padding: '8px 0', overflowX: 'auto', whiteSpace: 'pre' }}>
      {lines.map((l) => (
        <div key={l.n} style={{ display: 'flex' }}>
          <span className="faint num" style={{ width: 40, flex: 'none', textAlign: 'right', paddingRight: 10, userSelect: 'none' }}>{l.n}</span>
          <span>{l.text || ' '}</span>
        </div>
      ))}
    </div>
  );
}

export function RecordDetail({ record, kind, used, lineage, onPick, pid }) {
  const { members, rules } = splitMembers(record.fields);
  // 有校验规则记录时列记录（能点开、带置信度），没有时列字段上的 attrs.validations
  const checks = rules.length ? [] : recordValidations(record);
  const snippet = snippetLines(record);
  const link = (anchor) => <a className="link mono" style={{ fontSize: 12, wordBreak: 'break-all' }} onClick={() => onPick(anchor)}>{anchorTail(anchor)}</a>;
  const pairs = recordPairs(record).map(([k, v]) => [k, <span className={cx(k !== '来源' && 'mono')} style={{ fontSize: 12, wordBreak: 'break-all' }}>{v}</span>]);
  if (record.parent_anchor) pairs.unshift(['上级', link(record.parent_anchor)]);
  // 路由的处理方法（attrs.handler 是方法锚点）
  if (typeof record.attrs?.handler === 'string') pairs.push(['处理', link(record.attrs.handler)]);
  return (
    <>
      <div style={{ padding: '14px 14px 10px', borderBottom: '1px solid var(--line)' }}>
        <div className="row">
          <span style={{ width: 30, height: 30, borderRadius: 9, display: 'grid', placeItems: 'center', background: 'var(--t50)', color: 'var(--teal)', flex: 'none' }}><Icon name={recordIcon(record)} size={14} /></span>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="b lg ell">{record.name}</div>
            <div className="xs muted">{[record.label, recordKindText(record)].filter(Boolean).join(' · ')}</div>
          </div>
          {record.untrusted_content ? <UntrustedChip kind={kind} /> : null}
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
        {snippet ? <><div className="sh"><Icon name="code" size={12} />代码</div><Snippet lines={snippet} /></> : null}
        {record.attrs?.sql ? <><div className="sh"><Icon name="db" size={12} />SQL</div><div className="code" style={{ fontSize: 11 }}>{record.attrs.sql}</div></> : null}
        {checks.length || rules.length ? (
          <>
            <div className="sh"><Icon name="scale" size={12} />校验</div>
            {checks.map((c) => <div key={c} className="mono" style={{ fontSize: 12, padding: '3px 8px' }}>{c}</div>)}
            {rules.map((f) => (
              <div key={f.anchor} className="evd-fl" title="打开" onClick={() => onPick(f.anchor)}>
                <div><span className="mono b" style={{ fontSize: 12 }}>{f.name}</span><div className="xs muted">{f.type}</div></div>
                <div><ConfChip k={f.confidence} /></div>
              </div>
            ))}
          </>
        ) : null}
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
        {lineage === null ? <div className="xs muted">正在查…</div> : lineage.length ? lineage.map((l, i) => (
          <div className="evd-wu go" key={`${l.anchor}-${i}`} title="打开" onClick={() => onPick(l.anchor)}>
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
