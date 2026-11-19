// 插件自己的页面（ADR-0103）：左栏 id 是「包名:页面 id」；整块就是插件的隔离 iframe（PackFrame 页面模式），
// 标题、按钮都由插件自己画。插件没启用、页面不在注册表里时显示和内置插件页一样的空状态。
import { useMemo } from 'react';
import { useProject } from '../../app/context.js';
import PackFrame from '../../components/packs/PackFrame.jsx';
import { ShellP } from '../../shell/Shell.jsx';
import { PluginOff } from './parts.jsx';

export default function PluginFramePage({ route }) {
  const { extensions } = useProject();
  const key = route.page;
  const page = useMemo(() => (extensions?.pages || []).find((p) => p.id === key && p.ui), [extensions, key]);
  const nav = (extensions?.nav || []).find((n) => n.id === key);
  const off = (extensions?.disabled_pages || []).find((p) => p.id === key);
  let body = null;
  if (extensions && !page) body = <PluginOff label={off?.title || off?.pack_label || key.split(':')[0]} />;
  else if (page) {
    body = (
      <PackFrame
        key={`${page.pack}:${page.ui}`}
        editor={{ id: page.ui, pack: page.pack, title: nav?.title || page.page }}
        page={{ id: page.page, key, title: nav?.title || page.page }}
      />
    );
  }
  return (
    <ShellP active={key} flush bodyStyle={{ display: 'flex', flexDirection: 'column' }}>
      {body}
    </ShellP>
  );
}
