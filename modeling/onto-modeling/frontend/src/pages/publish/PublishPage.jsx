// 发布知识包与发布记录（设计稿 publish）
import { PAGE_TITLES } from '../../shell/CommandPalette.jsx';
import { PageHead, ShellP } from '../../shell/Shell.jsx';

export default function Page() {
  return (
    <ShellP active="publish">
      <PageHead title={PAGE_TITLES['publish']} desc="这一页正在接入。" />
    </ShellP>
  );
}
