// 业务系统数据源（系统逆向建模插件的资料子页，设计稿 sources）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="sources">
      <PageHead title={PAGE_TITLES['sources']} desc="这一页正在接入。" />
    </ShellP>
  );
}
