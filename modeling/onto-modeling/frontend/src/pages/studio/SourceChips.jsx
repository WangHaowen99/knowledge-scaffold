// 条目章节里的出处芯片（develop FileChip）：资料路径带锚点显示「文件名 · 位置」，点开去资料区按来源预览定位；
// 报告路径带小节打开报告原文并定位小节。资料已删除时芯片置灰，点了说明原因。跳过去的页面上方有「返回 <文档名>」
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { Chip } from '../../ui/index.jsx';
import { extensionOf, sourceChipLabel } from '../../lib/sourceLocation.js';
import { reportIdOf } from '../reports/reportModel.js';
import { sourceRefsIn, sourceTokens } from './studioModel.js';

const EXT_ICON = { xlsx: 'table', xlsm: 'table', xls: 'table', csv: 'table', png: 'image', jpg: 'image', jpeg: 'image' };

// 资料区里现有的资料路径（芯片置灰用）；取不到时不置灰
export function useKnownMaterials() {
  const { api } = useApp();
  const { pid } = useProject();
  const [known, setKnown] = useState(null);
  useEffect(() => {
    let alive = true;
    api.resources(pid)
      .then((r) => alive && setKnown(new Set((r.groups || []).flatMap((g) => (g.items || []).map((x) => x.path)))))
      .catch(() => alive && setKnown(null));
    return () => { alive = false; };
  }, [api, pid]);
  return known;
}

// 一个出处芯片：资料路径带锚点显示「文件名 · 位置」（资料已删除置灰，点了说明原因），报告路径带小节打开报告原文定位；
// 跳过去后「返回 <origin>」回到当前页面
export function SourceChip({ item, origin, known }) {
  const { pid } = useProject();
  const missing = item.type === 'source' && known && !known.has(item.path);
  const open = () => {
    const back = { from: window.location.hash.replace(/^#/, ''), fromLabel: origin || '' };
    if (item.type === 'heading') {
      navigate(pagePath('reports', pid), { report: reportIdOf(item.path), section: item.anchor, ...back });
    } else if (missing) {
      toast('文件不存在（可能已改名或删除）', 'info');
    } else {
      navigate(pagePath('resources', pid), { anchor: `file:${item.path}${item.anchor ? `#${item.anchor}` : ''}`, ...back });
    }
  };
  return (
    <Chip
      tone="line" sm icon={EXT_ICON[extensionOf(item.path)] || 'filetext'}
      className={`stt-src${missing ? ' missing' : ''}`} title={item.anchor ? `${item.path}#${item.anchor}` : item.path}
      role="button" tabIndex={0} onClick={(e) => { e.stopPropagation(); open(); }} onKeyDown={(e) => e.key === 'Enter' && open()}
    >
      {sourceChipLabel(item.path, item.anchor)}
    </Chip>
  );
}

// 条目章节下面的一排芯片（develop Studio 详情里正文的资料芯片）
export default function SourceChips({ text, origin, known }) {
  const refs = sourceRefsIn(text);
  if (!refs.length) return null;
  return (
    <div className="row wrap mt4" style={{ gap: 6 }}>
      {refs.map((ref) => <SourceChip key={`${ref.path}#${ref.anchor}`} item={ref} origin={origin} known={known} />)}
    </div>
  );
}

// 纯文本里就地换成芯片（报告原文、版本里的文件，develop 文件预览里的资料芯片）
export function SourceText({ text, origin, known }) {
  return sourceTokens(text).map((tok, i) => (tok.t === 'text'
    ? <span key={i}>{tok.v}</span>
    : <SourceChip key={i} item={{ type: tok.t, path: tok.path, anchor: tok.anchor }} origin={origin} known={known} />));
}
