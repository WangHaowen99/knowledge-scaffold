// 项目页的公共数据：元数据（P-04）、扩展项（PL-12）、待处理计数（IN-01）、流程（FL-01）、写入范围锁（CS-06），
// 加项目流（P-15）实时更新；断档时按 stream.reset 的 resync 重拉快照。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { errorText } from '../api/errors.js';
import { mergeFlow, normalizeFlow } from '../state/flow.js';
import { Button, Empty, KV, Note } from '../ui/index.jsx';
import { ShellG } from '../shell/Shell.jsx';
import { ProjectCtx, useApp } from './context.js';
import { anchorResolver } from './PopLayer.jsx';

export function ProjectArea({ pid, children }) {
  const { api, services, setMaintenance } = useApp();
  const [meta, setMeta] = useState(null);
  const [error, setError] = useState(null);
  const [extensions, setExtensions] = useState(null);
  const [inboxCount, setInboxCount] = useState(0);
  const [flow, setFlow] = useState(null);
  const [scope, setScope] = useState(null);
  const [recentProjects, setRecent] = useState([]);
  const subs = useRef(new Set());

  const loadMeta = useCallback(() => api.project(pid).then(setMeta), [api, pid]);
  const loadExt = useCallback(() => api.extensions(pid).then(setExtensions).catch(() => {}), [api, pid]);
  const loadInbox = useCallback(() => api.inbox(pid, { limit: 1 }).then((r) => setInboxCount(r.counts?.total || 0)).catch(() => {}), [api, pid]);
  const loadFlow = useCallback(() => api.flow(pid).then((f) => setFlow(normalizeFlow(f))).catch(() => {}), [api, pid]);
  const loadScope = useCallback(() => api.scope(pid).then(setScope).catch(() => {}), [api, pid]);

  useEffect(() => {
    setMeta(null);
    setError(null);
    loadMeta().then(() => {
      loadExt();
      loadInbox();
      loadFlow();
      loadScope();
    }).catch((e) => setError(e));
    api.projects({ limit: 8 }).then((r) => setRecent(r.items || [])).catch(() => {});
    anchorResolver.current = async (anchor) => {
      const r = await api.resolveAnchors(pid, [anchor]);
      return r.items?.[0] || null;
    };
    return () => { anchorResolver.current = null; };
  }, [api, pid, loadMeta, loadExt, loadInbox, loadFlow, loadScope]);

  const owned = !!meta && meta.package_name !== undefined;
  useEffect(() => {
    if (!owned) return undefined;
    const resync = { 'FL-01': loadFlow, 'IN-01': loadInbox, 'CS-06': loadScope, 'PL-12': loadExt };
    return services.openStream({
      kind: 'project',
      url: (q) => api.projectEventsUrl(pid, q),
      onReset: (d) => (d.resync || Object.keys(resync)).forEach((k) => resync[k]?.()),
      onEvent: (env) => {
        const d = env.data || {};
        switch (env.type) {
          case 'flow.updated': setFlow((f) => mergeFlow(f, d)); break;
          case 'inbox.updated': setInboxCount(d.counts?.total ?? 0); break;
          case 'scope.updated': setScope(d); break;
          case 'project.updated': setMeta(d.meta); break;
          case 'workspace.changed': setMeta((m) => (m && d.version_label ? { ...m, version_label: d.version_label, head: d.commit } : m)); break;
          case 'extensions.updated': loadExt(); break;
          case 'maintenance.updated': setMaintenance(d); break;
          default: break;
        }
        subs.current.forEach((fn) => fn(env));
      },
    });
  }, [owned, api, services, pid, loadFlow, loadInbox, loadScope, loadExt, setMaintenance]);

  const subscribe = useCallback((fn) => {
    subs.current.add(fn);
    return () => subs.current.delete(fn);
  }, []);

  const value = useMemo(() => ({
    pid, meta, extensions, inboxCount, flow, scope, recentProjects, subscribe, setMeta,
    reloadFlow: loadFlow, reloadScope: loadScope,
    hasPlugin: (id) => (extensions?.nav || []).some((n) => n.id === id),
    hasSubpage: (id) => (extensions?.subpages || []).some((s) => s.id === id),
    hasArtifactType: (t) => (extensions?.artifact_types || []).includes(t),
  }), [pid, meta, extensions, inboxCount, flow, scope, recentProjects, subscribe, loadFlow, loadScope]);

  if (error) return <ProjectError error={error} />;
  if (meta && !owned) return <MetaOnly meta={meta} />;
  return <ProjectCtx.Provider value={value}>{children}</ProjectCtx.Provider>;
}

function ProjectError({ error }) {
  const unowned = error.code === 'project_unowned';
  return (
    <ShellG active="projects">
      <div className="card meta-only" style={{ padding: 30 }}>
        <Empty
          icon={unowned ? 'user' : 'lock'}
          title={unowned ? '这个项目还没有所有者' : '打不开这个项目'}
          actions={<Button icon="arrowl" onClick={() => { window.location.hash = '#/projects'; }}>回到我的项目</Button>}
        >
          {unowned ? '项目的所有者还没有指定，管理员指定后才能打开。' : errorText(error)}
        </Empty>
      </div>
    </ShellG>
  );
}

// 管理员、运维看别人的项目：只有元数据（契约 1.4、ProjectMetaView）
function MetaOnly({ meta }) {
  return (
    <ShellG active="projects">
      <div className="meta-only">
        <div className="page-h"><div><div className="eyebrow">项目元数据</div><h1>{meta.name}</h1><p>这个项目归 {meta.owner || '—'} 所有。管理员和运维只能看元数据，不能打开项目内容。</p></div></div>
        <div className="card pad">
          <KV pairs={[
            ['所有者', meta.owner || '未指定'],
            ['阶段', meta.stage_label || '—'],
            ['构建方式', meta.build_mode === 'L4' ? '自主构建（L4）' : '陪伴建模（L2）'],
            ['状态', meta.archived ? '已归档' : '进行中'],
            ['创建时间', meta.created_at ? new Date(meta.created_at).toLocaleString('zh-CN') : '—'],
            ['最近活动', meta.last_activity_at ? new Date(meta.last_activity_at).toLocaleString('zh-CN') : '—'],
          ]} />
        </div>
        <Note tone="gray" className="mt12">项目里的目标、资料、产物和对话只有所有者能看到。</Note>
      </div>
    </ShellG>
  );
}
