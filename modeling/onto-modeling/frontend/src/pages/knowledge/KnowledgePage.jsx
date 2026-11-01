// 知识沉淀（插件页，设计稿 knowledge）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="knowledge">
      <PageHead title={PAGE_TITLES['knowledge']} desc="这一页正在接入。" />
    </ShellP>
  );
}
