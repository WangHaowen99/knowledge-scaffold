// 求解验证（develop SolverModelPanel 的 ValidationStatusCard，ADR-0098）：验证在发布后的独立评测里做，这里只显示当前修订
// 是否已发布并通过验证，给「去评测」与「导出验证用例（JSON）」。状态与去评测用评测对接的模型和入口（pages/evaluation）。
import { useState } from 'react';
import { useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { Button, Card, Chip, Modal } from '../../ui/index.jsx';
import { useReleaseEvaluations } from '../evaluation/ReleaseEvaluation.jsx';
import {
  SOLVER_VALIDATION_STATES, currentRevisionValidation, releaseSolverValidation, solverEvaluationTarget,
} from '../evaluation/evaluationModel.js';
import { needsValidation, shortRevision, validationText } from './solverValidationModel.js';

export default function SolverValidationCard({ view, dirty }) {
  const { pid } = useProject();
  const ev = useReleaseEvaluations(pid);
  const [confirm, setConfirm] = useState(false);
  if (!needsValidation(view)) return null;
  const status = ev.status;
  const current = currentRevisionValidation(status, view.revision);
  const latest = status?.current_release_id ? releaseSolverValidation(status, status.current_release_id) : null;
  const target = solverEvaluationTarget(status, view.revision, dirty);
  const passed = current.state === 'passed' && !dirty;
  const evaluate = () => ev.evaluate(target.release, 'solver');
  // 旧验证用例在本分支评测移出时已随启动迁移删除（ADR-0020），与 develop「没有保存过」同一提示
  const exportCases = () => toast('建模侧没有保存过验证用例', 'info');
  return (
    <Card title="求解验证" icon="badge" extra={<Chip sm tone={passed ? 'teal' : 'amber'}>{passed ? '当前修订已验证' : '待发布后验证'}</Chip>}>
      <div className="xs muted">修订 {shortRevision(view.revision)}</div>
      <div className="sm mt8">{validationText({ status, dirty, current })}</div>
      {latest ? (
        <div className="xs muted mt4">
          最近发布版本 {status.current_version}：{SOLVER_VALIDATION_STATES[latest.state].label}{latest.reason ? ` · ${latest.reason}` : ''}
        </div>
      ) : null}
      <div className="row g6 mt12">
        <Button size="sm" kind="primary" disabled={!target} onClick={() => (target?.stale ? setConfirm(true) : evaluate())}>去评测</Button>
        <Button size="sm" onClick={exportCases}>导出验证用例（JSON）</Button>
      </div>
      <div className="xs muted mt8">求解验证集在评测页面维护，可导入此处导出的旧验证用例。发布不再要求先通过验证，发布时需确认求解能力尚未验证。</div>
      <Modal open={confirm} onClose={() => setConfirm(false)} title="将评测不含当前修改的旧版本" w={480}
        foot={<><span className="sp" /><Button onClick={() => setConfirm(false)}>取消</Button>
          <Button kind="primary" onClick={() => { setConfirm(false); evaluate(); }}>仍去评测</Button></>}>
        <div className="sm">
          {`${dirty ? '有未保存的修改' : '当前修改尚未发布'}，评测页面将对最近发布的版本 ${target?.version || ''} 发起求解验证，结果不代表当前修改；要验证当前修改，请先${dirty ? '保存并' : ''}发布新版本。`}
        </div>
      </Modal>
    </Card>
  );
}
