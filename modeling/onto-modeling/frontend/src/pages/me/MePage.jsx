// 个人设置（设计稿 me）：左栏四个分区——账号（A-05～A-07）、外观与通知（A-08）、我的模型配置（A-09～A-11）。
// 地址参数 ?section=acct|look|noti|model，切分区时改地址、滚回顶部；顶栏 ⌘K 与项目设置「改我的模型配置」带 section=model 进来。
import { useEffect, useRef } from 'react';
import { navigate, pagePath } from '../../app/router.js';
import { ShellG } from '../../shell/Shell.jsx';
import { SNav } from '../../shell/SideNav.jsx';
import { AccountSection } from './AccountSection.jsx';
import { ModelSection } from './ModelSection.jsx';
import { LookSection, NotiSection, usePrefs } from './PrefsSections.jsx';
import { SECTIONS, parseSection } from './meModel.js';
import '../../styles/plugins.css';
import '../../styles/me.css';

export default function Page({ route }) {
  const section = parseSection(route.query?.section);
  const topRef = useRef(null);
  // 外观和通知共用一份偏好，切分区不重新读
  const prefs = usePrefs();

  useEffect(() => {
    const pb = topRef.current?.closest('.pg-body');
    if (pb) pb.scrollTop = 0;
  }, [section]);

  const side = (
    <SNav
      groups={[{ title: '个人', items: SECTIONS }]}
      active={section}
      onPick={(k) => { if (k !== section) navigate(pagePath('me'), { section: k }); }}
    />
  );

  return (
    <ShellG active="" side={side}>
      <section className="pl-pane" ref={topRef} data-ppane={section}>
        {section === 'acct' ? <AccountSection /> : null}
        {section === 'look' ? <LookSection prefs={prefs} /> : null}
        {section === 'noti' ? <NotiSection prefs={prefs} /> : null}
        {section === 'model' ? <ModelSection /> : null}
      </section>
    </ShellG>
  );
}
