// 可观测（运维中心，设计稿 ops-obs）
import { PageHead } from '../../shell/Shell.jsx';
import { OpsShell } from './parts.jsx';

export default function Page() {
  return (
    <OpsShell active="ops-obs">
      <PageHead title="可观测" desc="这一页正在接入。" />
    </OpsShell>
  );
}
