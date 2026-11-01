// 资料区（设计稿 resources）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="resources">
      <PageHead title={PAGE_TITLES['resources']} desc="这一页正在接入。" />
    </ShellP>
  );
}
