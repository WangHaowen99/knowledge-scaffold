// 插件运维（O-25～O-29）、core 默认版本（O-30～O-32）、包仓库备份（O-33、O-34）
import { useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useApp } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import {
  Button, Card, Chip, Icon, Inp, OverlayHostCtx, Prog, Seg, Table, useEscape,
} from '../../ui/index.jsx';
import { ConfirmModal, Missing, ViewChip } from './ctlParts.jsx';
import {
  BREAKER, CHANNEL, PACK_KIND, ROLLOUT_PRESETS, coreRows, errorRateView, fmtBytes, fmtDateTime, packActions, rolloutProblem,
} from './ctlModel.js';

// 「灰度 ▾」：按钮下方的小浮层（表格外框 overflow:hidden，浮层用 fixed 定位挂到弹层宿主）
function RolloutButton({ item, act, onApply, busy }) {
  const host = useContext(OverlayHostCtx);
  const btn = useRef(null);
  const pop = useRef(null);
  const [at, setAt] = useState(null);
  const [pct, setPct] = useState(String(item.rollout_pct ?? 0));
  const [by, setBy] = useState(item.rollout_by || 'project');
  const close = () => setAt(null);
  const place = () => {
    const r = btn.current.getBoundingClientRect();
    return { top: r.bottom + 6, left: Math.max(12, r.right - 280) };
  };
  const shown = !!at;
  useEscape(shown, close);
  // 点浮层和按钮以外的地方收起；页面滚动时跟着按钮走
  useEffect(() => {
    if (!shown) return undefined;
    const pb = btn.current?.closest('.pg-body');
    const down = (e) => { if (!pop.current?.contains(e.target) && !btn.current?.contains(e.target)) close(); };
    const follow = () => setAt(place());
    document.addEventListener('mousedown', down);
    pb?.addEventListener('scroll', follow, { passive: true });
    return () => { document.removeEventListener('mousedown', down); pb?.removeEventListener('scroll', follow); };
  }, [shown]);
  const open = () => {
    setPct(String(item.rollout_pct ?? 0));
    setBy(item.rollout_by || 'project');
    setAt(place());
  };
  const problem = rolloutProblem(pct);
  const node = at ? (
    <div className="menu oc-roll" ref={pop} style={{ position: 'fixed', top: at.top, left: at.left, zIndex: 120 }}>
      <div className="b sm">灰度 · <span className="mono">{item.name} {item.version}</span></div>
      <div className="xs muted mt4">按比例放给{by === 'tenant' ? '租户' : '项目'}，跟随通道的项目下一条消息生效</div>
      <div className="row mt8" style={{ gap: 4 }}>
        {ROLLOUT_PRESETS.map((p) => (
          <button key={p} type="button" className={`oc-preset${String(p) === pct ? ' on' : ''}`} onClick={() => setPct(String(p))}>{p}%</button>
        ))}
      </div>
      <div className="row mt8" style={{ gap: 8 }}>
        <Inp w="96px" value={pct} onChange={(e) => setPct(e.target.value.replace(/[^\d]/g, ''))} error={!!problem} inputMode="numeric" after={<span className="faint xs">%</span>} />
        <Seg items={[['project', '按项目'], ['tenant', '按租户']]} value={by} onChange={setBy} />
      </div>
      {problem ? <div className="xs mt4" style={{ color: 'var(--red)' }}>{problem}</div> : null}
      <div className="row mt12" style={{ justifyContent: 'flex-end', gap: 6 }}>
        <Button size="xs" onClick={close}>取消</Button>
        <Button size="xs" kind="primary" disabled={!!problem || busy} onClick={() => onApply(Number(pct), by).then((r) => r && close())}>应用</Button>
      </div>
    </div>
  ) : null;
  return (
    <>
      <span ref={btn} style={{ display: 'inline-flex' }}>
        <Button size="xs" disabled={act.disabled} title={act.title} after={<Icon name="chevd" size={12} />} onClick={() => (at ? close() : open())}>灰度</Button>
      </span>
      {node ? (host ? createPortal(node, host) : node) : null}
    </>
  );
}

export function PacksSection({
  packs, core, repo, perm, run, busy, reload,
}) {
  const { api } = useApp();
  const [dialog, setDialog] = useState(null); // { kind: 'rollback'|'disable'|'breaker'|'core', item }
  const close = () => setDialog(null);
  const after = (...keys) => (r) => { if (r) reload(...keys); return r; };

  const rollout = (it, percent, by) => run(`roll:${it.name}@${it.version}`, () => api.ops.setPackRollout(it.name, it.version, { percent, by }), {
    ok: `已把 ${it.name} ${it.version} 的灰度调到 ${percent}%，已写审计`,
  }).then(after('packs', 'core'));
  const rollback = (it) => run('pack', () => api.ops.rollbackPackChannel(it.name, { channel: it.channel, to_version: it.rollback_to }), {
    ok: `已把 ${it.name} 的 ${it.channel} 通道回滚到 ${it.rollback_to}，已写审计`,
  }).then(after('packs', 'core', 'versions'));
  const disable = (it, reason) => run('pack', () => api.ops.disablePackVersion(it.name, it.version, { reason }), {
    ok: `已禁用 ${it.name} ${it.version}，已写审计`,
  }).then(after('packs'));
  const breaker = (it, action, reason) => run('pack', () => api.ops.packBreaker(it.name, it.version, reason ? { action, reason } : { action }), {
    ok: action === 'open' ? `已熔断 ${it.name} ${it.version}，已写审计` : '已恢复，进入半开试探',
  }).then(after('packs'));
  const promote = (v) => run('core', () => api.ops.promoteCore({ version: v }), { ok: `已推广 core ${v} 到 stable，已写审计` }).then(after('core', 'packs', 'versions'));
  const rollbackCore = (v) => run('core', () => api.ops.rollbackCore({ version: v }), { ok: '已回滚默认版本，已写审计' }).then(after('core', 'packs', 'versions', 'repoBackups'));
  const backupRepo = () => run('repo', () => api.ops.backupPackRepo(), { ok: '已开始备份包仓库', icon: 'archive' }).then(after('tasks'));
  const deny = perm.opsWrite ? null : perm.hint('ops.write');

  const rows = (packs?.items || []).map((it) => {
    const a = packActions(it, perm.opsWrite);
    const ch = CHANNEL[it.channel] || { label: it.channel, tone: 'gray' };
    const er = errorRateView(it.error_rate_pct);
    return [
      <b className="mono" style={{ fontSize: 12.5 }}>{it.name}</b>,
      <span className="muted sm">{PACK_KIND[it.kind] || it.kind}</span>,
      <span className="tag">{it.version}</span>,
      <Chip tone={ch.tone} sm>{ch.label}</Chip>,
      <div className="row"><Prog p={it.rollout_pct} w="70px" thin /><span className="num xs">{it.rollout_pct ?? 0}%</span></div>,
      er.alert ? <span className="chip red sm">{er.text}</span> : <span className="num">{er.text}</span>,
      <ViewChip v={BREAKER[it.breaker]} />,
      <div className="row g4">
        <RolloutButton item={it} act={a.rollout} busy={busy === `roll:${it.name}@${it.version}`} onApply={(p, by) => rollout(it, p, by)} />
        <Button size="xs" disabled={a.rollback.disabled} title={a.rollback.title} onClick={() => setDialog({ kind: 'rollback', item: it })}>回滚</Button>
        <Button size="xs" kind="ghost" disabled={a.disable.disabled} title={a.disable.title} onClick={() => setDialog({ kind: 'disable', item: it })}>禁用</Button>
        <Button
          size="xs" kind={a.breaker.kind} disabled={a.breaker.disabled || busy === 'pack'} title={a.breaker.title}
          onClick={() => (a.breaker.action === 'open' ? setDialog({ kind: 'breaker', item: it }) : breaker(it, 'close'))}
        >
          {a.breaker.label}
        </Button>
      </div>,
    ];
  });

  const it = dialog?.item;
  return (
    <Card
      className="mt16" title="插件运维" icon="puzzle" sub="灰度、通道、回滚、禁用、出错熔断（可观测告警可自动触发熔断）" data-sec="packs"
      extra={<Button size="xs" onClick={() => navigate('/plugins')}>去插件中心</Button>}
    >
      <Table compact cols={['包', '类型', '版本', '通道', '灰度比例', '错误率', '熔断', '操作']} rows={rows} />
      {!packs ? <Missing /> : null}
      <div className="op-g12 mt16">
        <div className="card pad" style={{ background: 'var(--panel2)' }} data-sec="core">
          <div className="row mb8">
            <span style={{ color: 'var(--blue)' }}><Icon name="layers" size={14} /></span><b>core 默认版本</b><span className="sp" />
            {core?.stable ? <Chip tone="line" sm>stable · {core.stable}</Chip> : null}
            {core?.beta ? <Chip tone="indigo" sm>beta · {core.beta.version}（{core.beta.rollout_pct}%）</Chip> : null}
          </div>
          <Table
            compact
            cols={['版本', '通道', '发布', '操作']}
            rows={coreRows(core).map((v) => {
              const ch = CHANNEL[v.channel] || { label: v.channel, tone: 'gray' };
              let op = <span className="xs faint">当前默认</span>;
              if (v.op === 'promote') op = <Button size="xs" kind="soft" disabled={!perm.opsWrite || busy === 'core'} title={deny} onClick={() => promote(v.version)}>推广到 stable</Button>;
              if (v.op === 'rollback') op = <Button size="xs" disabled={!perm.opsWrite} title={deny} onClick={() => setDialog({ kind: 'core', item: v })}>回滚默认版本到此</Button>;
              return [<span className="tag">{v.version}</span>, <Chip tone={ch.tone} sm>{ch.label}</Chip>, <span className="muted">{fmtDateTime(v.released_at)}</span>, op];
            })}
          />
          {!core ? <Missing /> : null}
          <div className="xs muted mt8">切换是原子的；各项目仍按自己锁定的版本运行，下一条消息才生效。</div>
        </div>
        <div className="card pad" style={{ background: 'var(--panel2)' }}>
          <div className="row mb8">
            <span style={{ color: 'var(--blue)' }}><Icon name="archive" size={14} /></span><b>包仓库备份</b><span className="sp" />
            <Button size="xs" icon="archive" disabled={!perm.opsWrite || busy === 'repo'} title={deny} onClick={backupRepo}>立即备份</Button>
          </div>
          <Table
            compact
            cols={['文件', '大小', '时间']}
            rows={(repo?.items || []).map((b) => [<span className="mono xs">{b.name}</span>, fmtBytes(b.size), fmtDateTime(b.created_at)])}
          />
          {!repo ? <Missing /> : null}
          <div className="xs muted mt8">自动保留最近 {repo?.keep ?? 3} 份；回滚默认版本前会先备份一次。</div>
        </div>
      </div>

      <ConfirmModal
        open={dialog?.kind === 'rollback'} onClose={close} icon="history" title={it ? `把 ${it.name} 的 ${it.channel} 通道回滚到 ${it.rollback_to}？` : ''}
        sub="只改通道指针，已锁定版本的项目不受影响" noteTone="gray" noteIcon="info"
        note={it ? `跟随 ${it.channel} 通道的项目从下一条消息开始用 ${it.rollback_to}；${it.version} 仍留在包仓库里，可以再推广回来。` : ''}
        okText="回滚" onConfirm={() => rollback(it)}
      />
      <ConfirmModal
        open={dialog?.kind === 'disable'} onClose={close} danger icon="ban" title={it ? `禁用 ${it.name} ${it.version}？` : ''}
        sub="该版本不能再被新项目启用"
        note={it ? `已锁定它的 ${it.projects_using ?? 0} 个项目会进待处理，提示升级或回滚。` : ''}
        reason="required" placeholder="例如：节点失败率过高，等修复版本" okText="禁用" onConfirm={(reason) => disable(it, reason)}
      />
      <ConfirmModal
        open={dialog?.kind === 'breaker'} onClose={close} danger icon="ban" title={it ? `熔断 ${it.name} ${it.version}？` : ''}
        sub={it ? `${it.projects_using ?? 0} 个项目在用 · 错误率 ${errorRateView(it.error_rate_pct).text}` : ''}
        note="熔断后这个包的节点暂时跳过，发布关卡解除，错误留档里注明。恢复时先进入半开试探。"
        reason="optional" placeholder="例如：近 1 小时失败率超过阈值" okText="熔断" onConfirm={(reason) => breaker(it, 'open', reason)}
      />
      <ConfirmModal
        open={dialog?.kind === 'core'} onClose={close} icon="history" title={it ? `把 core 默认版本回滚到 ${it.version}？` : ''}
        sub="只改平台默认版本，已锁定版本的项目不受影响" noteTone="gray" noteIcon="info"
        note={it ? `回滚前会先备份包仓库。新建项目和选择「跟随默认」的项目，从下一条消息开始用 ${it.version}。` : ''}
        okText="回滚" onConfirm={() => rollbackCore(it.version)}
      />
    </Card>
  );
}
