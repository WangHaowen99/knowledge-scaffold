// 采集与快照（系统逆向建模插件的资料子页，设计稿 snapshots）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="snapshots">
      <PageHead title={PAGE_TITLES['snapshots']} desc="这一页正在接入。" />
    </ShellP>
  );
}
