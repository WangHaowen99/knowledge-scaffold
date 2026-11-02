// 系统设置（设计稿 sys）：只有管理员能用。左栏七个分区，一次显示一个，地址参数 ?section=model|ctx|sess|box|mcp|reg|quota
// （项目设置「添加服务器」跳 ?section=mcp）。其他角色打开地址看到整页说明，不发 admin 接口请求。
import { useEffect, useRef } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { ShellG } from '../../shell/Shell.jsx';
import { SNav } from '../../shell/SideNav.jsx';
import { ROLE_LABEL, can, rolesOf } from '../../state/roles.js';
import { Button, Empty } from '../../ui/index.jsx';
import { McpSection } from './McpSection.jsx';
import { ModelSection } from './ModelSection.jsx';
import { PresetsSection } from './PresetsSection.jsx';
import { QuotaSection } from './QuotaSection.jsx';
import { RegistrySection } from './RegistrySection.jsx';
import { SandboxSection, SessionSection } from './RuntimeSections.jsx';
import { SYS_SECTIONS, sysSection } from './sysModel.js';
import '../../styles/plugins.css';
import '../../styles/sys.css';

const PANES = {
  model: ModelSection, ctx: PresetsSection, sess: SessionSection, box: SandboxSection, mcp: McpSection, reg: RegistrySection, quota: QuotaSection,
};

function Denied() {
  const { user } = useApp();
  const isOps = rolesOf(user).includes('ops');
  const who = ROLE_LABEL[user?.role] || '建模者';
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
          你的账号是{who}，不能修改平台的模型接入、权限和配额。{isOps ? '会话池、维护模式这些运行参数在运维中心里调整。' : '需要时请联系管理员。'}
        </Empty>
      </div>
    </ShellG>
  );
}

export default function Page({ route }) {
  const { user } = useApp();
  const section = sysSection(route.query?.section);
  const rootRef = useRef(null);

  // 切分区时回到顶部（同设计稿 plSecMount）
  useEffect(() => {
    const pb = rootRef.current?.closest('.pg-body');
    if (pb) pb.scrollTop = 0;
  }, [section]);

  if (!can(user, 'admin')) return <Denied />;
  const Pane = PANES[section];
  const pick = (key) => navigate(pagePath('sys'), key === 'model' ? undefined : { section: key });
  return (
    <ShellG
      active=""
      side={<SNav groups={[{ title: '系统 · 管理员', items: SYS_SECTIONS }]} active={section} onPick={pick} />}
    >
      <section ref={rootRef} className={section === 'reg' ? 'pl-pane sy-wide' : 'pl-pane'} key={section}>
        <Pane onGo={pick} />
      </section>
    </ShellG>
  );
}
