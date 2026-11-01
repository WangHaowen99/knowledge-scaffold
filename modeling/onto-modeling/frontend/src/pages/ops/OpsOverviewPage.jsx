// 运维总览（运维中心，设计稿 ops）
import { PageHead } from '../../shell/Shell.jsx';
import { OpsShell } from './parts.jsx';

export default function Page() {
  return (
    <OpsShell active="ops">
      <PageHead title="运维总览" desc="这一页正在接入。" />
    </OpsShell>
  );
}
