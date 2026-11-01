// 用户、审计与回收站（运维中心，设计稿 ops-users）
import { PageHead } from '../../shell/Shell.jsx';
import { OpsShell } from './parts.jsx';

export default function Page() {
  return (
    <OpsShell active="ops-users">
      <PageHead title="用户、审计与回收站" desc="这一页正在接入。" />
    </OpsShell>
  );
}
