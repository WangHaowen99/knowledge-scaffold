// 运维管控与健康（运维中心，设计稿 ops-ctl）
import { PageHead } from '../../shell/Shell.jsx';
import { OpsShell } from './parts.jsx';

export default function Page() {
  return (
    <OpsShell active="ops-ctl">
      <PageHead title="运维管控与健康" desc="这一页正在接入。" />
    </OpsShell>
  );
}
