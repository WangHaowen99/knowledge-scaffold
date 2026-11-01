// 版本历史与对比（设计稿 versions）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="versions">
      <PageHead title={PAGE_TITLES['versions']} desc="这一页正在接入。" />
    </ShellP>
  );
}
