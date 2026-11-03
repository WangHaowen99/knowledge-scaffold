// 工作台上资料分析与建模蓝图的弹窗宿主（按路由懒加载）：资料地图、核验、调整蓝图、确认后怎么建
// dialog：'material_map' | 'verification' | 'blueprint' | {kind: 'build', next: 'L2'|'L4'}
import { BlueprintDialog } from './BlueprintDialog.jsx';
import { BuildFlow } from './BuildFlow.jsx';
import { MaterialMapDialog } from './MaterialMapDialog.jsx';
import { VerificationDrawer } from './VerificationDrawer.jsx';

export default function MaterialsDialogs({ dialog, onClose }) {
  const d = typeof dialog === 'string' ? { kind: dialog } : dialog || {};
  switch (d.kind) {
    case 'material_map': return <MaterialMapDialog onClose={onClose} />;
    case 'verification': return <VerificationDrawer onClose={onClose} />;
    case 'blueprint': return <BlueprintDialog onClose={onClose} />;
    case 'build': return <BuildFlow next={d.next === 'L4' ? 'L4' : 'L2'} onClose={onClose} />;
    default: return null;
  }
}
