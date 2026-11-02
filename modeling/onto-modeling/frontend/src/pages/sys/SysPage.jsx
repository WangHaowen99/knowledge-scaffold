// 系统设置（设计稿 sys）：只有管理员能用。左栏七个分区，一次显示一个，地址参数 ?section=model|ctx|sess|box|mcp|reg|quota
// （项目设置「添加服务器」跳 ?section=mcp）。其他角色打开地址看到整页说明，不发 admin 接口请求。
// 分区随接口出现（R17）：进来时读一次各分区的接口，回 404 的分区不进左栏，用地址直接打开也落到模型接入。
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { ShellG } from '../../shell/Shell.jsx';
import { SNav } from '../../shell/SideNav.jsx';
import { can, rolesOf } from '../../state/roles.js';
import { Button, Empty } from '../../ui/index.jsx';
import { Loading } from './parts.jsx';
import { McpSection } from './McpSection.jsx';
import { ModelSection } from './ModelSection.jsx';
import { PresetsSection } from './PresetsSection.jsx';
import { QuotaSection } from './QuotaSection.jsx';
import { RegistrySection } from './RegistrySection.jsx';
import { SandboxSection, SessionSection } from './RuntimeSections.jsx';
import { SECTION_PROBES, SYS_SECTIONS, availableSections, sysSection } from './sysModel.js';
import '../../styles/plugins.css';
import '../../styles/sys.css';

const PANES = {
  model: ModelSection, ctx: PresetsSection, sess: SessionSection, box: SandboxSection, mcp: McpSection, reg: RegistrySection, quota: QuotaSection,
};

function Denied() {
  const { user } = useApp();
  const isOps = rolesOf(user).includes('ops');
  return (
    <ShellG active="">
      <div className="card" style={{ padding: 30, maxWidth: 640, margin: '40px auto' }}>
        <Empty
          icon="lock"
          title="系统设置只对管理员开放"
          actions={(
            <>
              <Button icon="arrowl" onClick={() => navigate('/projects')}>回到我的项目</Button>
              {isOps ? <Button kind="soft" icon="gauge" onClick={() => navigate(pagePath('ops'))}>去运维中心</Button> : null}
            </>
          )}
        >
          {isOps ? null : '需要时请联系管理员'}
        </Empty>
      </div>
    </ShellG>
  );
}

// 读一次各分区的接口：{接口名: 'ok' | 'missing' | 'error'}；还没读完时为 null
function useSectionProbe(enabled) {
  const { api } = useApp();
  const [results, setResults] = useState(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    const names = [...new Set(Object.values(SECTION_PROBES).flat())];
    Promise.all(names.map((n) => api[n]().then(() => 'ok', (e) => (e?.status === 404 ? 'missing' : 'error'))))
      .then((states) => { if (alive) setResults(Object.fromEntries(names.map((n, i) => [n, states[i]]))); });
    return () => { alive = false; };
  }, [api, enabled]);
  return results;
}

export default function Page({ route }) {
  const { user } = useApp();
  const admin = can(user, 'admin');
  const probe = useSectionProbe(admin);
  const available = probe ? availableSections(probe) : null;
  const section = sysSection(route.query?.section, available);
  const rootRef = useRef(null);

  // 切分区时回到顶部（同设计稿 plSecMount）
  useEffect(() => {
    const pb = rootRef.current?.closest('.pg-body');
    if (pb) pb.scrollTop = 0;
  }, [section]);

  if (!admin) return <Denied />;
  const Pane = PANES[section];
  const pick = (key) => navigate(pagePath('sys'), key === 'model' ? undefined : { section: key });
  const items = available ? SYS_SECTIONS.filter((s) => available.includes(s.key)) : [];
  return (
    <ShellG
      active=""
      side={<SNav groups={[{ items }]} active={section} onPick={pick} />}
    >
      <section ref={rootRef} className={section === 'reg' ? 'pl-pane sy-wide' : 'pl-pane'} key={section}>
        {probe ? <Pane onGo={pick} /> : <Loading />}
      </section>
    </ShellG>
  );
}
