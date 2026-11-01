// 代码生成与求解（插件页，设计稿 scripts）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="scripts">
      <PageHead title={PAGE_TITLES['scripts']} desc="这一页正在接入。" />
    </ShellP>
  );
}
