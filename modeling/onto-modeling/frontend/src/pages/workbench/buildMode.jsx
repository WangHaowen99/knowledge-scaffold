// 对话里切换构建方式（F2 第 8 条）：输入框旁的模式胶囊点开选陪伴建模或自主构建；项目设置里的入口保留
import { useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { MODE_LABEL, modeChipLabel, planModeSwitch, researchDone } from './buildModeModel.js';

/**
 * @param {object} o
 * @param {object} o.ab useAutobuild 的返回
 * @param {(dialog: object|string) => void} o.openDialog 打开工作台的资料 / 蓝图弹窗（{kind: 'build', next: 'L4'} 走开始前预览）
 */
export function useModeSwitch({ ab, openDialog }) {
  const { api } = useApp();
  const { pid, meta, setMeta, flow } = useProject();
  const [busy, setBusy] = useState(false);
  const runState = ab.active ? ab.state : null;
  const saved = meta?.build_mode || 'L2';
  const mode = runState && !ab.ended ? 'L4' : saved;

  const save = async (m) => {
    if (saved === m) return;
    const r = await api.setBuildMode(pid, m);
    setMeta((x) => (x ? { ...x, build_mode: r.build_mode } : x));
  };
  const switchTo = async (target) => {
    if (busy) return;
    setBusy(true);
    try {
      switch (planModeSwitch({ target, mode, runState, researchDone: researchDone(flow) })) {
        case 'busy': toast('运行正在停下，停下后再切', 'clock'); break;
        case 'convert': if (await ab.act('convert_to_l2')) await save('L2'); break;
        case 'set': await save(target); toast(`已切到${MODE_LABEL[target]}`, 'check'); break;
        case 'set_wait_research': await save('L4'); toast('已切到自主构建，资料分析做完后在这里开始', 'rocket'); break;
        case 'set_then_build': await save('L4'); openDialog({ kind: 'build', next: 'L4' }); break;
        default: break;
      }
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  return { mode, label: modeChipLabel({ mode, runState }), switchTo, busy };
}
