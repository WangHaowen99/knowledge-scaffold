// 场景走查（插件页，设计稿 scenarios）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="scenarios">
      <PageHead title={PAGE_TITLES['scenarios']} desc="这一页正在接入。" />
    </ShellP>
  );
}
