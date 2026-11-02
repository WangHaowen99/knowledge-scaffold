// 运维中心 4 页共用（设计稿 pages-70-ops.js 的 opNav）：左栏（身份卡、4 项导航与角标、监听说明）、角色把关。
// 运维管理面接口走 api.ops.*（契约 1.1：{ops_base_url}/api/v1/ops/...）。
// 角色（契约 1.3、src/state/roles.js）：ops 读写，admin 只读（用户管理与回收站除外），user 打不开。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate } from '../../app/router.js';
import { ShellG } from '../../shell/Shell.jsx';
import { SNav } from '../../shell/SideNav.jsx';
import { can, deniedHint, opsListenLabel, opsRoleCard } from '../../state/roles.js';
import { Avatar, Button, Chip, Empty, Icon } from '../../ui/index.jsx';
import '../../styles/ops.css';

// O-13 ready.status → 左栏「运维管控与健康」的角标
const READY_BADGE = { degraded: ['降级', 'amber'], not_ready: ['未就绪', 'red'] };

function OpsNav({ active }) {
  const { api, user, config } = useApp();
  const [firing, setFiring] = useState(0);
  const [ready, setReady] = useState(null);
  useEffect(() => {
    let alive = true;
    api.ops.alertRules().then((r) => alive && setFiring(r.firing || 0)).catch(() => {});
    api.ops.health().then((r) => alive && setReady(r.ready?.status || null)).catch(() => {});
    return () => { alive = false; };
  }, [api]);
  const card = opsRoleCard(user);
  const badge = READY_BADGE[ready];
  const port = opsListenLabel(config?.ops_base_url);
  const initial = (user?.display_name || user?.username || '?').slice(0, 1);
  return (
    <SNav
      active={active}
      top={card ? (
        <div className="op-role">
          <div className="row"><Avatar text={initial} i={2} size="sm" /><b>{user?.display_name || user?.username}</b><Chip tone="indigo" sm>{card.label}</Chip></div>
          {card.text ? <div className="mt4">{card.text}</div> : null}
        </div>
      ) : null}
      groups={[{
        items: [
          { key: 'ops', title: '总览', icon: 'gauge', go: 'ops' },
          { key: 'ops-obs', title: '可观测', icon: 'activity', go: 'ops-obs', extra: firing ? <Chip tone="red" sm>{firing}</Chip> : null },
          { key: 'ops-ctl', title: '运维管控与健康', icon: 'wrench', go: 'ops-ctl', extra: badge ? <Chip tone={badge[1]} sm>{badge[0]}</Chip> : null },
          { key: 'ops-users', title: '用户与审计', icon: 'users', go: 'ops-users' },
        ],
      }]}
      bottom={(
        <div className="op-foot">
          <Icon name="lock" size={12} /> <b>运维管理面</b>
          {port ? <> · 内网端口 <span className="mono">{port}</span></> : ' · 独立监听'}
        </div>
      )}
    />
  );
}

/**
 * 运维中心页面外壳：没有运维面权限时整页说明，不发运维接口请求。
 * children 可以是函数 (perm) => 节点，perm = { opsWrite, usersWrite, trashWrite, hint(ability) }。
 */
export function OpsShell({ active, children, bodyStyle }) {
  const { user } = useApp();
  const perm = useOpsPerm();
  if (!can(user, 'ops.read')) {
    return (
      <ShellG active="ops">
        <div className="card" style={{ padding: 30, maxWidth: 640, margin: '40px auto' }}>
          <Empty icon="lock" title="运维中心只对运维和管理员开放" actions={<Button icon="arrowl" onClick={() => navigate('/projects')}>回到我的项目</Button>}>
            需要时请联系管理员
          </Empty>
        </div>
      </ShellG>
    );
  }
  return (
    <ShellG active="ops" side={<OpsNav active={active} />} bodyStyle={bodyStyle}>
      {typeof children === 'function' ? children(perm) : children}
    </ShellG>
  );
}

// 当前用户在运维中心能做什么；按钮用 disabled + title={perm.hint(...)} 表示只读
export function useOpsPerm() {
  const { user } = useApp();
  return {
    opsWrite: can(user, 'ops.write'),
    usersWrite: can(user, 'users.write'),
    trashWrite: can(user, 'trash.write'),
    hint: deniedHint,
  };
}
