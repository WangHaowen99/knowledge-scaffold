// 项目设置（设计稿 proj-settings）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="proj-settings">
      <PageHead title={PAGE_TITLES['proj-settings']} desc="这一页正在接入。" />
    </ShellP>
  );
}
