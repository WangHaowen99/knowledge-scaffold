// 条目章节里的出处芯片（develop FileChip）：资料路径带锚点显示「文件名 · 位置」，点开去资料区按来源预览定位；
// 报告路径带小节打开报告原文并定位小节。资料已删除时芯片置灰，点了说明原因。跳过去的页面上方有「返回 <文档名>」
import { useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { Chip } from '../../ui/index.jsx';
import { extensionOf, sourceChipLabel } from '../../lib/sourceLocation.js';
import { reportIdOf } from '../reports/reportModel.js';
import { sourceRefsIn } from './studioModel.js';

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

export default function SourceChips({ text, origin, known }) {
  const { pid } = useProject();
  const refs = sourceRefsIn(text);
  if (!refs.length) return null;
  // 跳过去后「返回 <文档名>」回到这里
  const back = { from: window.location.hash.replace(/^#/, ''), fromLabel: origin || '' };
  const open = (ref) => {
    if (ref.type === 'heading') {
      navigate(pagePath('reports', pid), { report: reportIdOf(ref.path), section: ref.anchor, ...back });
      return;
    }
    if (known && !known.has(ref.path)) {
      toast('文件不存在（可能已改名或删除）', 'info');
      return;
    }
    navigate(pagePath('resources', pid), { anchor: `file:${ref.path}${ref.anchor ? `#${ref.anchor}` : ''}`, ...back });
  };
  return (
    <div className="row wrap mt4" style={{ gap: 6 }}>
      {refs.map((ref) => {
        const missing = ref.type === 'source' && known && !known.has(ref.path);
        return (
          <Chip
            key={`${ref.path}#${ref.anchor}`} tone="line" sm icon={EXT_ICON[extensionOf(ref.path)] || 'filetext'}
            className={`stt-src${missing ? ' missing' : ''}`} title={ref.anchor ? `${ref.path}#${ref.anchor}` : ref.path}
            role="button" tabIndex={0} onClick={() => open(ref)} onKeyDown={(e) => e.key === 'Enter' && open(ref)}
          >
            {sourceChipLabel(ref.path, ref.anchor)}
          </Chip>
        );
      })}
    </div>
  );
}
