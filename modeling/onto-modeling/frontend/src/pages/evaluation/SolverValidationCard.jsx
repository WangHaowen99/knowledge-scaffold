// 求解契约所在页的「求解验证」卡（照 develop SolverModelPanel 的 ValidationStatusCard）：求解验证在发布后的独立评测中
// 进行，这里只显示当前修订是否已发布并通过验证，并提供去评测（与导出旧验证用例）的入口。
// 用法：<SolverValidationCard pid={pid} revision={求解契约修订} dirty={有未保存修改} show={需要发布后验证} onExportCases={可选} />
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { Button, Chip, Modal } from '../../ui/index.jsx';
import { message } from './EvaluationDoc.jsx';
import { openEvaluation, prefetchEvaluationEntry } from './evaluationEntry.js';
import {
  PACKAGE_PUBLISHED, SOLVER_VALIDATION_STATES, currentRevisionValidation, releaseSolverValidation, solverEvaluationTarget,
} from './evaluationModel.js';
import { shortRevision, solverValidationText } from './evaluationText.js';
import '../../styles/evaluation.css';

export function SolverValidationCard({ pid, revision, dirty = false, show = true, reloadKey = 0, onExportCases }) {
  const { api, services } = useApp();
  const [status, setStatus] = useState(null);
  const [published, setPublished] = useState(0);
  const [confirming, setConfirming] = useState(false);
  const [exporting, setExporting] = useState(false);
  // 在本页打开时从别处发布：按发布通知重读，否则「去评测」仍指向发布前的版本
  useEffect(() => {
    const onPublished = (event) => { if (event.detail?.projectId === pid) setPublished((n) => n + 1); };
    window.addEventListener(PACKAGE_PUBLISHED, onPublished);
    return () => window.removeEventListener(PACKAGE_PUBLISHED, onPublished);
  }, [pid]);
  useEffect(() => {
    if (!show || !pid) return undefined;
    let alive = true;
    api.evaluations(pid).then((value) => alive && setStatus(value)).catch(() => alive && setStatus({ configured: false }));
    return () => { alive = false; };
  }, [api, pid, show, reloadKey, published]);
  // 点击「去评测」时要立即打开窗口：先取好评测入口地址
  useEffect(() => { prefetchEvaluationEntry(api.evaluationEntry).catch(() => {}); }, [api]);
  if (!show) return null;
  const current = currentRevisionValidation(status, revision);
  const latest = status?.current_release_id ? releaseSolverValidation(status, status.current_release_id) : null;
  const target = solverEvaluationTarget(status, revision, dirty);
  const passed = current.state === 'passed' && !dirty;
  const evaluate = () => openEvaluation(target.release, {
    loadEntry: api.evaluationEntry, runType: 'solver',
    tokens: () => ({ access_token: services.tokens.access, refresh_token: services.tokens.refresh }),
  }).catch((e) => toast(message(e), 'alert'));
  const exportCases = async () => {
    setExporting(true);
    try { await onExportCases(); } catch (e) { toast(message(e), 'alert'); } finally { setExporting(false); }
  };
  return (
    <div className="solver-validation">
      <div className="row mb8">
        <b>求解验证</b>
        <Chip sm tone={passed ? 'teal' : 'amber'}>{passed ? '当前修订已验证' : '待发布后验证'}</Chip>
        <span className="xs muted">修订 {shortRevision(revision)}</span>
      </div>
      <div className="sm mb8">{solverValidationText(status, revision, dirty)}</div>
      {latest ? (
        <div className="xs muted mb8">
          最近发布版本 {status.current_version}：{SOLVER_VALIDATION_STATES[latest.state].label}{latest.reason ? ` · ${latest.reason}` : ''}
        </div>
      ) : null}
      <div className="row mb8" style={{ gap: 6 }}>
        <Button size="sm" kind="primary" disabled={!target} onClick={target?.stale ? () => setConfirming(true) : evaluate}>去评测</Button>
        {onExportCases ? <Button size="sm" disabled={exporting} onClick={exportCases}>导出验证用例（JSON）</Button> : null}
      </div>
      <div className="xs muted">求解验证集在评测页面维护，可导入此处导出的旧验证用例。发布不再要求先通过验证，发布时需确认求解能力尚未验证。</div>
      <Modal open={confirming} onClose={() => setConfirming(false)} w={480} title="将评测不含当前修改的旧版本"
        foot={(
          <>
            <span className="sp" />
            <Button onClick={() => setConfirming(false)}>取消</Button>
            {/* 确定按钮的点击里同步打开评测窗口 */}
            <Button kind="primary" onClick={() => { setConfirming(false); evaluate(); }}>仍去评测</Button>
          </>
        )}>
        <div className="sm">
          {`${dirty ? '有未保存的修改' : '当前修改尚未发布'}，评测页面将对最近发布的版本 ${target?.version || ''} 发起求解验证，结果不代表当前修改；要验证当前修改，请先${dirty ? '保存并' : ''}发布新版本。`}
        </div>
      </Modal>
    </div>
  );
}
