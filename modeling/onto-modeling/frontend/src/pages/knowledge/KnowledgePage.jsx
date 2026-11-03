// 知识沉淀（插件页，设计稿 knowledge）：项目层 + 行业包两层知识、新增 / 编辑 / 删除、冲突检查、提名回流。
// 插件只管沉淀，知识的存放和加载在 core；「在对话里复核」只发命令（S-20），页面显示进度，结果回到冲突检查卡。
// 写入范围内的条目只读：助手运行中 / 修改待确认时「编辑」「删除」停用（契约 EP-03、ScopeLock 里 type=core.knowledge）。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import {
  Button, Card, Chip, Empty, Ev, Icon, Inp, Seg, Select, cx,
} from '../../ui/index.jsx';
import {
  CommandRunBar, ExtraActions, PluginEyebrow, PluginOff, ScopeLockBar, SendButton, useCommandTasks, usePageEnabled, useProjectEvent,
} from '../plugin/parts.jsx';
import {
  CONFIDENCE, KN_TYPES, LAYER_BADGE, LAYER_CHIP, TYPE_FILTER, checkSub, conflictChip, conflictLine, emptyForm, entryLocked,
  footText, formFromDetail, isEmptyProject, layerInfo, layerSeg, listQuery, lockCopy, materialsLine, overrideForm, overrideParts,
  quoteMeta, reviewCommand, richBlocks, scopeLabel, sourceMeta, totalFor, usedByLabel, usedByTarget,
} from './knowledgeModel.js';
import { ConflictsModal, DeleteModal, EntryFormModal, NominateModal } from './KnowledgeDialogs.jsx';
import '../../styles/knowledge.css';

const PAGE_SIZE = 20;

export default function KnowledgePage({ route }) {
  const enabled = usePageEnabled('knowledge');
  return (
    <ShellP active="knowledge">
      {enabled === false ? <PluginOff label="知识沉淀" what="知识条目" /> : <Knowledge route={route} />}
    </ShellP>
  );
}

function Knowledge({ route }) {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const [layer, setLayer] = useState('all');
  const [type, setType] = useState('');
  const [q, setQ] = useState('');
  const [qd, setQd] = useState('');
  const [resp, setResp] = useState(null);
  const [items, setItems] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [sel, setSel] = useState(route?.query?.id || null);
  // 地址带 ?id= 时第一次加载保留它（可能不在第一页）
  const routeId = useRef(route?.query?.id || null);
  const [detail, setDetail] = useState(null);
  const [check, setCheck] = useState(null);
  const [convTitles, setConvTitles] = useState({});
  const [dialog, setDialog] = useState(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  const loadCheck = useCallback(() => api.knowledgeConflicts(pid).then(setCheck).catch(() => {}), [api, pid]);
  const tasks = useCommandTasks('knowledge', { onFinish: () => { reload(); loadCheck(); } });

  useEffect(() => { const t = setTimeout(() => setQd(q), 220); return () => clearTimeout(t); }, [q]);

  // K-01：筛选变了重拉第一页
  useEffect(() => {
    let alive = true;
    api.knowledge(pid, listQuery({ layer, type, q: qd, limit: PAGE_SIZE })).then((r) => {
      if (!alive) return;
      setResp(r);
      setItems(r.items || []);
      setCursor(r.next_cursor || null);
      const keep = routeId.current;
      routeId.current = null;
      setSel((cur) => (cur && ((r.items || []).some((e) => e.id === cur) || cur === keep) ? cur : r.items?.[0]?.id || null));
    }).catch((e) => toast(errorText(e), 'alert'));
    return () => { alive = false; };
  }, [api, pid, layer, type, qd, tick]);

  useEffect(() => { loadCheck(); }, [loadCheck, tick]);
  useEffect(() => {
    api.conversations(pid).then((r) => setConvTitles(Object.fromEntries((r.items || []).map((c) => [c.id, c.title])))).catch(() => {});
  }, [api, pid]);

  // K-02：选中的条目
  useEffect(() => {
    if (!sel) { setDetail(null); return undefined; }
    let alive = true;
    api.knowledgeEntry(pid, sel).then((d) => { if (alive) setDetail(d); }).catch(() => { if (alive) setDetail(null); });
    return () => { alive = false; };
  }, [api, pid, sel, tick]);

  useProjectEvent('knowledge.updated', () => reload());

  const loadMore = () => api.knowledge(pid, listQuery({ layer, type, q: qd, cursor, limit: PAGE_SIZE })).then((r) => {
    setItems((x) => [...x, ...(r.items || [])]);
    setCursor(r.next_cursor || null);
  }).catch((e) => toast(errorText(e), 'alert'));

  const info = layerInfo(resp);
  const industryPack = useMemo(() => (info.industry.pack ? { name: info.industry.pack, label: info.industry.label } : null), [info.industry.pack, info.industry.label]);
  const empty = isEmptyProject(resp);
  const total = totalFor(resp?.counts, layer);

  const openAdd = (form = emptyForm()) => setDialog({ kind: 'add', form });
  const onSaved = (r, mode) => {
    setDialog(null);
    const conflicts = r.conflicts || [];
    if (mode === 'edit') toast(`已保存为版本 ${r.version_label}`, 'check');
    else if (conflicts.length) toast(`已入库（${r.version_label}）· 与「${conflicts[0].with}」口径不同，助手正在复核`, 'shield');
    else toast(`已入库（${r.version_label}）`, 'check');
    if (r.entry?.id) setSel(r.entry.id);
    reload();
  };
  const onDeleted = (r) => {
    setDialog(null);
    toast(`已删除，保存为版本 ${r.version_label}${r.restored_industry_entry ? `；行业包条目「${r.restored_industry_entry}」重新生效` : ''}`, 'check');
    setSel(null);
    reload();
  };
  const nominate = async (id) => {
    try {
      const d = detail?.id === id ? detail : await api.knowledgeEntry(pid, id);
      setDialog({ kind: 'nominate', detail: d });
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  return (
    <>
      <PageHead
        eyebrow={<PluginEyebrow pack="knowledge" label="知识沉淀" />}
        title="知识沉淀"
        actions={(
          <>
            <ExtraActions page="knowledge" tasks={tasks} />
            <Button kind="primary" icon="plus" onClick={() => openAdd()}>新增条目</Button>
          </>
        )}
      />
      {tasks.active ? (
        <div className="mr-runrow" style={{ margin: '0 0 14px' }}>
          <CommandRunBar task={tasks.active} />
        </div>
      ) : null}
      {empty ? (
        <div className="card">
          <Empty
            icon="book"
            title="还没有沉淀的业务知识"
            actions={(
              <>
                <Button kind="primary" icon="plus" onClick={() => openAdd()}>新增条目</Button>
                <Button icon="msg" onClick={() => navigate(pagePath('wb-l2', pid))}>去对话里沉淀</Button>
              </>
            )}
          />
        </div>
      ) : (
        <div className="mr-2c">
          <div>
            <div className="row mb12">
              <Seg items={layerSeg(resp?.counts).map(([k, t, pop]) => [k, pop ? <span data-pop={pop}>{t}</span> : t])} value={layer} onChange={setLayer} />
              <Inp icon="search" w="220px" placeholder="搜索条目" value={q} onChange={(e) => setQ(e.target.value)} />
              <span className="sp" />
              <Select w="130px" icon="filter" options={TYPE_FILTER} value={type} onChange={setType} />
            </div>
            <div className="mr-kl">
              {items.map((e) => <Item key={e.id} entry={e} on={e.id === sel} lock={scope} onPick={() => setSel(e.id)} />)}
            </div>
            {resp && !items.length ? <div className="card kn-none"><Empty icon="search" title="没有符合条件的条目" /></div> : null}
            {resp ? (
              <div className="row mt12 muted sm">
                <Icon name="info" size={14} />{footText(items.length, total)}
                {cursor ? <Button size="xs" kind="ghost" onClick={loadMore}>加载更多</Button> : null}
              </div>
            ) : null}
          </div>
          <div className="col" style={{ gap: 16 }}>
            {detail ? (
              <Detail
                d={detail}
                lock={scope}
                convTitles={convTitles}
                packLabel={info.industry.label}
                onEdit={() => setDialog({ kind: 'edit', form: formFromDetail(detail), detail })}
                onDelete={() => setDialog({ kind: 'delete', detail })}
                onNominate={() => setDialog({ kind: 'nominate', detail })}
                onOverride={() => openAdd(overrideForm(detail))}
              />
            ) : null}
            <ConflictCard check={check} tasks={tasks} onOpen={() => setDialog({ kind: 'conflicts' })} />
          </div>
        </div>
      )}
      <EntryFormModal
        open={dialog?.kind === 'add' || dialog?.kind === 'edit'}
        mode={dialog?.kind}
        initial={dialog?.form}
        detail={dialog?.detail}
        onClose={() => setDialog(null)}
        onSaved={onSaved}
      />
      <DeleteModal open={dialog?.kind === 'delete'} detail={dialog?.detail} onClose={() => setDialog(null)} onDeleted={onDeleted} />
      <NominateModal open={dialog?.kind === 'nominate'} detail={dialog?.detail} pack={industryPack} onClose={() => setDialog(null)} />
      <ConflictsModal open={dialog?.kind === 'conflicts'} check={check} onClose={() => setDialog(null)} onNominate={nominate} />
    </>
  );
}

function Item({ entry: e, on, lock, onPick }) {
  const [badge, badgeCls] = LAYER_BADGE[e.layer] || LAYER_BADGE.project;
  const [typeLabel, tone] = KN_TYPES[e.type] || [e.type, 'gray'];
  const src = sourceMeta(e.source);
  const copy = entryLocked(lock, e) ? lockCopy(lock) : null;
  return (
    <div className={cx('mr-ki', on && 'on')} data-id={e.id} onClick={onPick}>
      <span className={cx('ly', badgeCls)}>{badge}</span>
      <div className="grow">
        <div className="row"><span className="t grow">{e.title}</span><Chip tone={tone} sm>{typeLabel}</Chip></div>
        <div className="m">
          <span className="row g4"><Icon name="target" size={12} />{scopeLabel(e.scope)}</span>
          <span className="row g4"><Icon name="gauge" size={12} />置信度 {CONFIDENCE[e.confidence]?.[0] || '—'}</span>
          <span className="row g4"><Icon name={src.icon} size={12} />{src.text}</span>
          {e.overrides_industry ? <span className="chip amber sm"><Icon name="layers" />覆盖行业包同名条目</span> : null}
          {copy ? <span className="kn-lk" data-pop={copy.pop}><Icon name="lock" size={12} />只读</span> : null}
        </div>
      </div>
    </div>
  );
}

// 正文：段落、列表、[[条目]]（点了去 Studio 表格按名称搜）
function Rich({ md }) {
  const { pid } = useProject();
  const parts = (ps) => ps.map((p, i) => {
    if (p.t === 'link') return <span key={i} className="wl" title="在 Studio 中查看" onClick={() => navigate(pagePath('studio-table', pid), { q: p.v })}>[[{p.v}]]</span>;
    if (p.t === 'b') return <b key={i}>{p.v}</b>;
    return <span key={i}>{p.v}</span>;
  });
  return (
    <div className="sm kn-rich">
      {richBlocks(md).map((b, i) => {
        if (b.t === 'li') return <div key={i} className="kn-li">{parts(b.parts)}</div>;
        if (b.t === 'h') return <div key={i} className="b">{parts(b.parts)}</div>;
        return <p key={i}>{parts(b.parts)}</p>;
      })}
    </div>
  );
}

function Detail({ d, lock, convTitles, packLabel, onEdit, onDelete, onNominate, onOverride }) {
  const { pid } = useProject();
  const [typeLabel, tone] = KN_TYPES[d.type] || [d.type, 'gray'];
  const [layerLabel, layerTone] = LAYER_CHIP[d.layer] || LAYER_CHIP.project;
  const [conf, confTone] = CONFIDENCE[d.confidence] || ['—', 'gray'];
  const locked = entryLocked(lock, d);
  const copy = locked ? lockCopy(lock) : null;
  const ov = overrideParts(d.overrides, packLabel);
  const project = d.layer === 'project' && !d.readonly;
  const quote = d.source_quote;
  return (
    <Card title={d.title} extra={<Chip tone={tone} sm>{typeLabel}</Chip>}>
      <div className="row wrap g4">
        <Chip tone={layerTone} sm>{layerLabel}</Chip>
        <Chip tone={confTone} sm>置信度 {conf}</Chip>
        <Chip tone="line" sm icon="target">{scopeLabel(d.scope)}</Chip>
      </div>
      {d.background_md ? <><div className="mr-sec">背景</div><Rich md={d.background_md} /></> : null}
      <div className="mr-sec">规则</div>
      <Rich md={d.rule_md} />
      {quote || d.source?.kind === 'material' ? (
        <>
          <div className="mr-sec">来源</div>
          {quote ? (
            <div className="mr-quote">
              「{quote.text}」
              <div className="xs faint mt4">
                {quote.conversation_id ? quoteMeta(quote, convTitles) : d.source?.anchor ? <Ev anchor={d.source.anchor} label={d.source.label} /> : d.source?.label}
              </div>
            </div>
          ) : <Ev anchor={d.source.anchor} label={d.source.label} />}
        </>
      ) : null}
      {ov ? (
        <div className="note amber mt12">
          <Icon name="layers" />
          <div>覆盖 <b>{ov.target}</b>{ov.note}</div>
        </div>
      ) : null}
      {d.used_by?.length ? (
        <>
          <div className="mr-sec">被哪些产物用到</div>
          <div className="row wrap g4">
            {d.used_by.map((ref) => {
              const t = usedByTarget(ref);
              return (
                <Chip key={ref.id || `${ref.type}/${ref.key}`} tone="line" sm className={t ? 'kn-link' : undefined} onClick={t ? () => navigate(pagePath(t.page, pid), t.query) : undefined}>
                  {usedByLabel(ref)}
                </Chip>
              );
            })}
          </div>
        </>
      ) : null}
      {copy ? <ScopeLockBar lock={lock} text={copy.text} className="mt12" style={{ marginBottom: 0 }} /> : null}
      {project ? (
        <div className="row mt16">
          <span data-pop={copy?.pop}><Button size="sm" icon="edit" disabled={locked} onClick={onEdit}>编辑</Button></span>
          <Button size="sm" icon="upload" kind="soft" disabled={!packLabel} onClick={onNominate}>提名回流行业包</Button>
          <span className="sp" />
          <span data-pop={copy?.pop}><Button size="sm" icon="trash" kind="danger" disabled={locked} onClick={onDelete}>删除</Button></span>
        </div>
      ) : (
        <div className="row mt16">
          <span className="sp" />
          <Button size="sm" icon="layers" kind="soft" onClick={onOverride}>写同名条目覆盖</Button>
        </div>
      )}
    </Card>
  );
}

// 知识冲突检查卡：口径冲突发到对话里复核；跑完结果回到这里。右上的计数点开明细
function ConflictCard({ check, tasks, onOpen }) {
  if (!check) return null;
  const conflicts = check.conflicts || [];
  const clean = !(check.duplicates || check.broken_refs);
  const chip = conflictChip(check);
  return (
    <Card
      title="知识冲突检查"
      icon="shield"
      sub={checkSub(check)}
      extra={chip ? <Chip tone={chip.tone} sm className="kn-link" title="查看明细" onClick={onOpen}>{chip.text}<Icon name="chev" size={12} /></Chip> : null}
    >
      <div className="col" style={{ gap: 8 }}>
        {conflicts.map((c) => {
          const cmd = reviewCommand(c);
          return (
            <div className="row g12 sm" key={c.id || c.project_entry?.id}>
              <span style={{ color: 'var(--amber2)' }}><Icon name="alert" size={14} /></span>
              <div className="grow">
                {conflictLine(c)}
                {c.kind === 'scope_conflict' ? (
                  <div className="mt8">
                    <SendButton tasks={tasks} command={cmd.command} args={cmd.args} text={cmd.text} size="xs" kind="soft" icon="msg" disabled={!!tasks.active}>在对话里复核</SendButton>
                  </div>
                ) : null}
              </div>
            </div>
          );
        })}
        <div className="row g12 sm">
          <span style={{ color: 'var(--blue)' }}><Icon name="check" size={14} /></span>
          <div className="grow">{materialsLine(check)}</div>
        </div>
        <div className="row g12 sm">
          <span style={{ color: clean ? 'var(--blue)' : 'var(--amber2)' }}><Icon name={clean ? 'check' : 'alert'} size={14} /></span>
          <div className="grow">重复条目：{check.duplicates ?? 0} · 失效引用：{check.broken_refs ?? 0}</div>
        </div>
      </div>
    </Card>
  );
}
