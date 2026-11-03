// 对话里切换构建方式（F2 第 8 条）：输入框旁的模式胶囊点开选陪伴建模或自主构建；项目设置里的入口保留
import { useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Modal } from '../../ui/index.jsx';
import { MODE_LABEL, modeChipLabel, planModeSwitch } from './buildModeModel.js';

async function currentBlueprint(api, pid) {
  try {
    return await api.blueprint(pid);
  } catch (e) {
    if (e.code === 'blueprint_not_found') return null;
    throw e;
  }
}

/**
 * @param {object} o
 * @param {object} o.ab useAutobuild 的返回
 * @param {(dialog: object|string) => void} o.openDialog 打开工作台的资料 / 蓝图弹窗（{kind: 'build', next: 'L4'} 走开始前预览）
 */
export function useModeSwitch({ ab, openDialog }) {
  const { api } = useApp();
  const { pid, meta, setMeta } = useProject();
  const [guide, setGuide] = useState(null);
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
      const bp = target === 'L4' && !(runState && !ab.ended) ? await currentBlueprint(api, pid) : null;
      switch (planModeSwitch({ target, mode, runState, blueprintState: bp?.state || null })) {
        case 'busy': toast('运行正在停下，停下后再切', 'clock'); break;
        case 'convert': if (await ab.act('convert_to_l2')) await save('L2'); break;
        case 'set': await save(target); toast(`已切到${MODE_LABEL[target]}`, 'check'); break;
        case 'set_wait_blueprint': await save('L4'); toast('已切到自主构建，确认蓝图后开始', 'rocket'); break;
        case 'set_then_confirm': await save('L4'); setGuide(bp); break;
        case 'set_then_build': await save('L4'); openDialog({ kind: 'build', next: 'L4' }); break;
        default: break;
      }
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setBusy(false);
    }
  };
  const guideEl = guide ? (
    <BlueprintGuide
      bp={guide}
      onClose={() => setGuide(null)}
      onAdjust={() => { setGuide(null); openDialog('blueprint'); }}
      onConfirm={() => { setGuide(null); openDialog({ kind: 'build', next: 'L4' }); }}
    />
  ) : null;
  return { mode, label: modeChipLabel({ mode, runState }), switchTo, busy, guideEl };
}

// 切到自主构建时蓝图还没确认：先看一眼切片再确认，确认后接着开始前预览（BP-03 next=L4）
function BlueprintGuide({ bp, onClose, onAdjust, onConfirm }) {
  const slices = (bp.slices || []).filter((s) => !s.excluded);
  return (
    <Modal
      open onClose={onClose} icon="layers" title="先确认建模蓝图" w={540}
      foot={(
        <>
          <a className="link xs grow" onClick={onAdjust}>调整切片…</a>
          <Button onClick={onClose}>取消</Button>
          <Button kind="primary" icon="check" onClick={onConfirm}>确认蓝图</Button>
        </>
      )}
    >
      <div className="row wrap" style={{ gap: 6 }}>
        {slices.map((s) => <Chip key={s.id} tone="line" sm>{s.name}</Chip>)}
      </div>
    </Modal>
  );
}
