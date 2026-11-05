// 插件中心（设计稿 plugins）：PL-01 目录（带「启用到」的项目算本项目视角的状态与推荐）、PL-05 待生效的是什么、
// 启用（PL-03 → PL-06）、更新（PL-03 → 更新弹窗或直接 PL-07）、提供方安装（管理员 PL-19，其他人 PL-04）。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { navigate, pagePath } from '../../app/router.js';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellG } from '../../shell/Shell.jsx';
import {
  Button, Empty, Inp, Note, Select,
} from '../../ui/index.jsx';
import { PackCard, RecBar } from './catalogCards.jsx';
import { usePackActions } from './catalogDialogs.jsx';
import { catOf, devSkillOf, sectionsOf, visibleCatalog } from './catalogModel.js';
import { PluginsNav, useEvidenceOn, usePluginProject } from './parts.jsx';

export default function PluginsPage({ route }) {
  const { api, services } = useApp();
  const { projectId, setProjectId, projects, ready } = usePluginProject(route);
  const cat = catOf(route.query);
  const devSkill = devSkillOf(cat);
  // ?pack= 来自助手推荐卡：搜索框先填上这个包
  const [q, setQ] = useState(route.query?.pack || '');
  const [raw, setData] = useState(null);
  const evidenceOn = useEvidenceOn();
  const data = visibleCatalog(raw, evidenceOn);
  const [error, setError] = useState(null);
  const [pending, setPending] = useState({});
  // 推荐只在换项目时取一次：处理过的那条留着，按钮换成「已启用」「已更新」（设计稿）
  const [recs, setRecs] = useState({ pid: undefined, items: [] });
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!ready) return;
    const n = ++seq.current;
    try {
      const [r, pp] = await Promise.all([
        api.packs(projectId ? { project_id: projectId } : undefined),
        projectId ? api.projectPacks(projectId).catch(() => null) : null,
      ]);
      if (n !== seq.current) return;
      setData(r);
      setError(null);
      setPending(Object.fromEntries((pp?.items || []).filter((p) => p.pending).map((p) => [p.name, p.pending])));
      setRecs((s) => (s.pid === projectId ? s : { pid: projectId, items: r.recommendations || [] }));
    } catch (e) {
      if (n === seq.current) setError(errorText(e));
    }
  }, [api, projectId, ready]);
  useEffect(() => { load(); }, [load]);

  const projectName = projects.find((p) => p.id === projectId)?.name;
  const actions = usePackActions({ projectId, projectName, onChanged: load });
  const items = data?.items || [];
  const sections = sectionsOf(items, { cat, q });
  const recItems = visibleCatalog({ recommendations: recs.items }, evidenceOn).recommendations || [];
  const pickCat = (k) => {
    const rest = { ...(route.query || {}) };
    delete rest.cat;
    navigate(pagePath('plugins'), k === 'all' ? rest : { ...rest, cat: k });
  };

  return (
    <ShellG active="plugins" side={<PluginsNav active={cat} counts={data?.counts || {}} onCat={pickCat} projectId={projectId} />}>
      {/* 管理员装进平台给所有人用，普通用户装给自己用（ADR-0079）；都从这里进 */}
      <PageHead
        title="插件中心"
        actions={(
          <>
            {devSkill ? (
              <Button icon="download" onClick={() => services.download(devSkill.path, devSkill.filename).catch((e) => toast(errorText(e), 'alert'))}>
                下载开发 Skill
              </Button>
            ) : null}
            <Button kind="primary" icon="upload" onClick={() => navigate(pagePath('plugin-upload'))}>安装插件</Button>
          </>
        )}
      />
      <div className="pl-bar">
        <Inp w={300} icon="search" placeholder="搜索插件名称、ID 或用途…" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="sp" />
        <span className="xs muted">启用到</span>
        {projects.length ? (
          <Select w="230px" icon="pkg" value={projectId} options={projects.map((p) => ({ value: p.id, label: p.name }))} onChange={setProjectId} />
        ) : <span className="xs faint">{ready ? '还没有项目' : ''}</span>}
      </div>
      {recs.pid === projectId && recItems.length ? <RecBar recs={recItems} items={items} busy={actions.busy} onAction={actions.act} /> : null}
      {error ? (
        <Note tone="red" className="mt16">插件目录没有读出来：{error} <Button size="xs" onClick={load}>重试</Button></Note>
      ) : null}
      {!data && !error ? <div className="row mt16 sm muted"><span className="spin" />正在读取插件目录…</div> : null}
      {data ? sections.map((s) => (
        <div key={s.key}>
          <div className="pl-sec"><h3>{s.title}</h3></div>
          <div className="pl-grid">
            {s.items.map((p) => (
              <PackCard
                key={p.name}
                p={p}
                projectId={projectId}
                pendingKind={pending[p.name]?.kind}
                requested={actions.requested.has(p.name)}
                busy={actions.busy === p.name}
                onAction={actions.act}
              />
            ))}
          </div>
        </div>
      )) : null}
      {data && !sections.length ? <Empty icon="search" title="没有匹配的插件" /> : null}
      {actions.dialogs}
    </ShellG>
  );
}
