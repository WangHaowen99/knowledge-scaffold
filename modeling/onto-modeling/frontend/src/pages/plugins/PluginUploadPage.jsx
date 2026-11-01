// 上传与审核（设计稿 plugin-upload）
import { PageHead, ShellG } from '../../shell/Shell.jsx';
import { PluginsNav } from './parts.jsx';

export default function Page() {
  return (
    <ShellG active="plugins" side={<PluginsNav active="up" />} >
      <PageHead title="上传与审核" desc="这一页正在接入。" />
    </ShellG>
  );
}
