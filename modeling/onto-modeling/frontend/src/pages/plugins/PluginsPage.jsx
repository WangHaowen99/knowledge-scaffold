// 插件中心（设计稿 plugins）
import { PageHead, ShellG } from '../../shell/Shell.jsx';
import { PluginsNav } from './parts.jsx';

export default function Page() {
  return (
    <ShellG active="plugins" side={<PluginsNav active="all" />} >
      <PageHead title="插件中心" desc="这一页正在接入。" />
    </ShellG>
  );
}
