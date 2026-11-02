// 项目设置（设计稿 proj-settings）：左侧分区导航 + 基本信息、引擎与模型、构建方式、自主构建策略（含开始入口）、
// 能力与插件、助手可用的 MCP、对外 MCP 接口、危险操作。
// 地址参数：?section=basic|engine|build-mode|autobuild|plugins|mcp-servers|mcp|danger 滚到并高亮对应分区；
// ?dialog=start 直接走「开始自主构建」（AB-03 预览 → 确认 → AB-04），⌘K 用它。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';
import { Icon, Note, cx } from '../../ui/index.jsx';
import { useProjectEvent } from '../plugin/parts.jsx';
import { BasicSection, EngineSection } from './BasicEngine.jsx';
import { BuildSection } from './BuildSection.jsx';
import { DangerSection } from './DangerSection.jsx';
import { McpOutSection, McpServersSection } from './McpSections.jsx';
import { PacksSection } from './PacksSection.jsx';
import { SECTIONS, activeSection, sectionTarget } from './settingsModel.js';
import '../../styles/evidence.css';
import '../../styles/settings.css';

const RUNNING = ['queued', 'running', 'pausing', 'paused', 'stopping', 'converting'];

// 一次取齐各分区的数据（都到了再渲染，地址参数定位时位置才稳）
function useSettingsData(pid) {
  const { api } = useApp();
  const { flow } = useProject();
  const flowRef = useRef(flow);
  flowRef.current = flow;
  const [d, setD] = useState(null);
  const put = useCallback((k, v) => setD((x) => ({ ...x, [k]: typeof v === 'function' ? v(x?.[k]) : v })), []);
  const loaders = useRef({});
  loaders.current = {
    engines: () => api.engines(),
    engine: () => api.engine(pid),
    model: () => api.modelSettings(),
    buildMode: () => api.buildMode(pid),
    policy: () => api.autobuildPolicy(pid),
    runs: () => api.autobuildRuns(pid),
    // 没有蓝图（FL-01 blueprint.state === 'none'）时不读 BP-01，免得每次进设置都多一个 404
    blueprint: async () => {
      const f = flowRef.current || await api.flow(pid).catch(() => null);
      if (f?.blueprint?.state === 'none') return null;
      return api.blueprint(pid).catch((e) => (e.code === 'blueprint_not_found' ? null : Promise.reject(e)));
    },
    sources: () => api.evidenceSources({ project_id: pid }),
    packs: () => api.projectPacks(pid),
    assembly: () => api.assembly(pid),
    servers: () => api.projectMcpServers(pid),
    mcp: () => api.mcp(pid),
    tokens: () => api.mcpTokens(pid),
  };
  const reload = useCallback(async (...keys) => {
    const res = await Promise.allSettled(keys.map((k) => loaders.current[k]()));
    setD((x) => {
      const out = { ...x };
      keys.forEach((k, i) => { out[k] = res[i].status === 'fulfilled' ? res[i].value : (x?.[k] ?? null); });
      return out;
    });
  }, []);
  useEffect(() => {
    let alive = true;
    setD(null);
    const keys = Object.keys(loaders.current);
    Promise.allSettled(keys.map((k) => loaders.current[k]())).then((res) => {
      if (!alive) return;
      const out = {};
      keys.forEach((k, i) => { out[k] = res[i].status === 'fulfilled' ? res[i].value : null; });
      setD(out);
    });
    return () => { alive = false; };
  }, [pid]);
  return [d, put, reload];
}

export default function Page({ route }) {
  const { pid, meta } = useProject();
  const [d, put, reload] = useSettingsData(pid);
  const rootRef = useRef(null);
  const [nav, setNav] = useState('basic');
  const [autoStart, setAutoStart] = useState(false);
  const spyLock = useRef(0);
  // 点了导航、页面却滚不到那么远（最后几个分区）时，滚到底仍高亮点的那一项
  const pinned = useRef(null);

  useProjectEvent('assembly.updated', () => reload('packs', 'assembly'));
  useProjectEvent(['autobuild.run.updated', 'autobuild.finished'], () => reload('runs', 'policy'));

  const body = () => rootRef.current?.closest('.pg-body');
  const topOf = (el, pb) => el.getBoundingClientRect().top - pb.getBoundingClientRect().top + pb.scrollTop;
  const scrollTo = useCallback((anchor, { flash = false, smooth = true, pin = null } = {}) => {
    const pb = body();
    const el = rootRef.current?.querySelector(`[data-sec="${anchor}"]`);
    if (!pb || !el) return;
    spyLock.current = Date.now() + 700;
    pinned.current = pin;
    pb.scrollTo({ top: Math.max(0, topOf(el, pb) - 16), behavior: smooth ? 'smooth' : 'auto' });
    if (flash) {
      el.classList.remove('out');
      el.classList.add('ps-hl');
      setTimeout(() => el.classList.add('out'), 800);
      setTimeout(() => el.classList.remove('ps-hl', 'out'), 1900);
    }
  }, []);

  // 滚动时左侧导航跟着走
  const ready = !!d && !!meta;
  useEffect(() => {
    const pb = body();
    if (!pb || !ready) return undefined;
    let last = pb.scrollTop;
    const on = () => {
      const up = pb.scrollTop < last;
      last = pb.scrollTop;
      if (Date.now() < spyLock.current) return;
      if (up) pinned.current = null;
      const atBottom = pb.scrollTop + pb.clientHeight >= pb.scrollHeight - 4;
      if (atBottom && pinned.current) { setNav(pinned.current); return; }
      const tops = SECTIONS.map((s) => {
        const el = rootRef.current?.querySelector(`[data-sec="${s.id}"]`);
        return el ? [s.id, topOf(el, pb)] : null;
      }).filter(Boolean);
      const cur = activeSection(tops, pb.scrollTop);
      if (cur) setNav(cur);
    };
    pb.addEventListener('scroll', on, { passive: true });
    return () => pb.removeEventListener('scroll', on);
  }, [ready]);

  // 地址参数：滚到分区并闪一下；dialog=start 交给构建方式分区开始自主构建；处理完把参数去掉
  const section = route.query?.section;
  const dialog = route.query?.dialog;
  useEffect(() => {
    if (!ready || (!section && !dialog)) return undefined;
    const t = sectionTarget(section || (dialog === 'start' ? 'autobuild' : null));
    const timer = setTimeout(() => {
      if (t) {
        setNav(t.nav);
        scrollTo(t.anchor, { flash: true, smooth: false, pin: t.nav });
      }
      if (dialog === 'start') setAutoStart(true);
      navigate(pagePath('proj-settings', pid));
    }, 60);
    return () => clearTimeout(timer);
  }, [ready, section, dialog, pid, scrollTo]);

  const active = (d?.runs?.items || []).find((r) => RUNNING.includes(r.state))
    || (d?.buildMode?.autobuild_running ? { run_id: d.buildMode.run_id, run_no: null, state: 'running' } : null);
  const archived = !!meta?.archived;

  return (
    <ShellP active="proj-settings">
      <PageHead title={PAGE_TITLES['proj-settings']} />
      <div className="evd-ps" ref={rootRef}>
        <nav className="evd-pn">
          {SECTIONS.map((s) => (
            <a key={s.id} className={cx(nav === s.id && 'on')} onClick={() => { setNav(s.id); scrollTo(s.id, { pin: s.id }); }}>
              <Icon name={s.icon} size={14} />{s.title}
            </a>
          ))}
        </nav>
        <div className="evd-pc">
          {!ready ? <div className="card pad muted sm">正在读取项目设置…</div> : (
            <>
              {archived ? <Note tone="amber" icon="archive">项目已归档，只读。在「危险操作」里恢复。</Note> : null}
              <BasicSection archived={archived} />
              <EngineSection d={d} put={put} archived={archived} active={active} />
              <BuildSection d={d} put={put} reload={reload} archived={archived} active={active} autoStart={autoStart} onAutoStarted={() => setAutoStart(false)} />
              <PacksSection d={d} reload={reload} archived={archived} />
              <McpServersSection d={d} put={put} archived={archived} />
              <McpOutSection d={d} reload={reload} />
              <DangerSection archived={archived} />
            </>
          )}
        </div>
      </div>
    </ShellP>
  );
}
