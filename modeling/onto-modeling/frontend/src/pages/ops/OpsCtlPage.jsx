// 运维管控与健康（运维中心，设计稿 ops-ctl）：健康与依赖、版本与资源、会话池、维护模式、插件运维与 core 默认版本、
// 启动自检、备份与恢复演练、任务、版本清单、数据保留、诊断包。接口 O-13～O-47（契约 3.25），C-03 / C-04 只显示路径。
// 角色：ops 读写，admin 只读（写按钮 disabled + title；O-37 立即备份、O-47 下载 admin 也可）。
// 地址参数 ?section=health|sessions|maintenance|packs|core|startup|backups|tasks|versions|retention|diagnostics：滚到该分区并闪一下（运维总览「完整报告」用 startup）。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Icon } from '../../ui/index.jsx';
import { useAct, useUserNames } from './ctlParts.jsx';
import { HealthSection } from './ctlHealth.jsx';
import { EnterMaintModal, MaintenanceCard } from './ctlMaint.jsx';
import { BackupsCard, StartupCard, TasksCard } from './ctlRecovery.jsx';
import { DiagnosticsCard, RetentionCard, VersionsCard } from './ctlMisc.jsx';
import { PacksSection } from './ctlPacks.jsx';
import { SessionsSection } from './ctlSessions.jsx';
import { currentStep, isLiveTask, reloadsAfter, sectionOf } from './ctlModel.js';
import { OpsShell } from './parts.jsx';
import '../../styles/ops-ctl.css';

// 维护推进、排空时 3 秒一次；只有后台任务在跑时 8 秒一次
const POLL_FAST_MS = 3000;
const POLL_TASKS_MS = 8000;

function useCtlData() {
  const { api } = useApp();
  const [d, setD] = useState(null);
  const loaders = useRef({});
  loaders.current = {
    health: () => api.ops.health(),
    sessions: () => api.ops.sessions(),
    maint: () => api.ops.maintenance(),
    packs: () => api.ops.packs(),
    core: () => api.ops.coreDefault(),
    repoBackups: () => api.ops.packRepoBackups(),
    startup: () => api.ops.startupReport(),
    backups: () => api.ops.backups(),
    drills: () => api.ops.drills(),
    tasks: () => api.ops.tasks(),
    versions: () => api.ops.versions(),
    retention: () => api.ops.retention(),
    diags: () => api.ops.diagnostics(),
  };
  const reload = useCallback(async (...keys) => {
    const res = await Promise.allSettled(keys.map((k) => loaders.current[k]()));
    setD((x) => {
      const out = { ...x };
      keys.forEach((k, i) => { if (res[i].status === 'fulfilled') out[k] = res[i].value; });
      return out;
    });
    return res;
  }, []);
  const put = useCallback((k, v) => setD((x) => ({ ...x, [k]: v })), []);
  useEffect(() => {
    let alive = true;
    const keys = Object.keys(loaders.current);
    Promise.allSettled(keys.map((k) => loaders.current[k]())).then((res) => {
      if (!alive) return;
      const out = {};
      const failed = [];
      keys.forEach((k, i) => {
        out[k] = res[i].status === 'fulfilled' ? res[i].value : null;
        if (res[i].status === 'rejected') failed.push(res[i].reason);
      });
      setD(out);
      if (failed.length) toast(`有 ${failed.length} 块没读到：${errorText(failed[0])}`, 'alert');
    });
    return () => { alive = false; };
  }, []);
  return [d, reload, put];
}

function CtlBody({ route, perm }) {
  const { api, maintenance: appMaint } = useApp();
  const [d, reload, put] = useCtlData();
  const names = useUserNames();
  const [busy, run] = useAct();
  const [enter, setEnter] = useState(false);
  const rootRef = useRef(null);
  const ready = !!d;

  // 维护状态：向导里改完直接用返回值；别处（另一个运维、用户流 maintenance.updated）改了也跟着刷新
  const onMaint = useCallback((st) => { put('maint', st?.active ? st : { active: false }); reload('sessions', 'core', 'packs', 'versions'); }, [put, reload]);
  const appActive = !!appMaint?.active;
  const firstApp = useRef(true);
  useEffect(() => {
    if (firstApp.current) { firstApp.current = false; return; }
    reload('maint', 'sessions');
  }, [appActive, reload]);

  // 轮询：维护在排空 / 升级、刚发起排空，或有进行中的后台任务时；任务结束后刷新对应的那一块
  const cur = currentStep(d?.maint);
  const liveTasks = (d?.tasks?.items || []).some(isLiveTask);
  const [drainWatch, setDrainWatch] = useState(false);
  const hasRunning = (d?.sessions?.items || []).some((x) => x.state === 'running');
  const draining = hasRunning && (!!cur || drainWatch);
  useEffect(() => { if (!hasRunning) setDrainWatch(false); }, [hasRunning]);
  const prevTasks = useRef(null);
  useEffect(() => { prevTasks.current = d?.tasks?.items || null; }, [d?.tasks]);
  useEffect(() => {
    const needMaint = cur && cur.progress_pct != null && cur.progress_pct < 100;
    if (!needMaint && !liveTasks && !draining) return undefined;
    const t = setInterval(async () => {
      const before = prevTasks.current;
      const keys = ['tasks'];
      if (needMaint || draining) keys.push('maint', 'sessions');
      const res = await reload(...keys);
      const next = res[0].status === 'fulfilled' ? res[0].value.items : null;
      const more = reloadsAfter(before, next);
      if (cur?.id === 'upgrade') more.push('core', 'packs', 'versions');
      if (more.length) reload(...new Set(more));
    }, needMaint || draining ? POLL_FAST_MS : POLL_TASKS_MS);
    return () => clearInterval(t);
  }, [cur?.id, cur?.progress_pct, liveTasks, draining, reload]);

  // ?section= 滚到分区并闪一下，处理完去掉参数
  const section = sectionOf(route.query?.section);
  useEffect(() => {
    if (!ready || !section) return undefined;
    const timer = setTimeout(() => {
      const el = rootRef.current?.querySelector(`[data-sec="${section}"]`);
      const pb = rootRef.current?.closest('.pg-body');
      if (el && pb) {
        const top = el.getBoundingClientRect().top - pb.getBoundingClientRect().top + pb.scrollTop;
        pb.scrollTo({ top: Math.max(0, top - 16), behavior: 'auto' });
        el.classList.add('oc-hl');
        setTimeout(() => el.classList.add('out'), 900);
        setTimeout(() => el.classList.remove('oc-hl', 'out'), 2000);
      }
      navigate('/ops-ctl');
    }, 80);
    return () => clearTimeout(timer);
  }, [ready, section]);

  const probe = () => run('probe', () => api.ops.probeHealth(), { ok: (r) => `已重新探测 ${(r || []).length} 个依赖` })
    .then((r) => { if (r) put('health', { ...d.health, dependencies: r }); });
  return (
    <div ref={rootRef}>
      <PageHead title="运维管控与健康" />
      {!ready ? <div className="card pad muted sm">正在读取运维状态…</div> : (
        <>
          <HealthSection health={d.health} startup={d.startup} perm={perm} busy={busy} onProbe={probe} />
          <SessionsSection pool={d.sessions} names={names} perm={perm} run={run} busy={busy} reload={reload} onDrained={() => setDrainWatch(true)} />
          <div className="sec-t mt24"><Icon name="wrench" size={14} />管控</div>
          <MaintenanceCard state={d.maint} perm={perm} run={run} busy={busy} onChanged={onMaint} onEnter={() => setEnter(true)} />
          <PacksSection packs={d.packs} core={d.core} repo={d.repoBackups} perm={perm} run={run} busy={busy} reload={reload} />
          <div className="op-g12 mt16">
            <StartupCard report={d.startup} />
            <BackupsCard backups={d.backups} drills={d.drills} perm={perm} run={run} busy={busy} reload={reload} />
          </div>
          <TasksCard tasks={d.tasks} names={names} perm={perm} reload={reload} />
          <div className="op-g12 mt16">
            <VersionsCard versions={d.versions} />
            <div className="col" style={{ gap: 16 }}>
              <RetentionCard retention={d.retention} perm={perm} run={run} busy={busy} onSaved={(r) => put('retention', r)} />
              <DiagnosticsCard diags={d.diags} names={names} perm={perm} run={run} busy={busy} reload={reload} />
            </div>
          </div>
          <EnterMaintModal open={enter} onClose={() => setEnter(false)} core={d.core} run={run} busy={busy} onEntered={onMaint} />
        </>
      )}
    </div>
  );
}

export default function Page({ route }) {
  return (
    <OpsShell active="ops-ctl">
      {(perm) => <CtlBody route={route} perm={perm} />}
    </OpsShell>
  );
}
