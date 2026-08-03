import { useEffect, useState } from 'react';
import {
  ignorePackageCheckFinding,
  repairAiConstructionKnowledge,
  reviewPackageCheckFinding,
} from './authoring-api.js';

// 知识包体检面板（知识包完善页顶部）：规则引擎 + 语义 agent 的结构化发现。
//
// 这里只负责「列出发现」。每条发现的处理——修改和忽略——都交给建模助手：
// 忽略要写审计记录、要填理由，在面板里就地填表既和助手里的处理路径重复，
// 刷新还会丢；助手那边已经有卡片和待办两套现成的承载方式。
//
// 自动重查由 PackageWorkspace 统一驱动并经 props 下发：这个面板只在「编辑与检查」页签下
// 挂载，如果重查跟着它走，评测和诊断页签下改包就不会自动校验。
const SEV_TAG = { 错误: 'tag-err', 警告: 'tag-draft', 提示: 'tag-lang' };

export default function PackageCheck({
  projectId,
  onOpenFile,
  packageDraft = null,
  check: suppliedCheck = null,
  errorDeferral: suppliedErrorDeferral = null,
  warningReview: suppliedWarningReview = null,
  repair: repairAvailability = null,
  rechecking = false,
  error: suppliedError = '',
}) {
  const [manualWarningReview, setManualWarningReview] = useState(null);
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState('');
  const [handoffId, setHandoffId] = useState('');
  const [repairing, setRepairing] = useState(false);

  // 助手侧的处理结果回流前，先本地生效，避免闪回旧结果
  const check = suppliedCheck;
  const errorDeferral = suppliedErrorDeferral;
  const warningReview = manualWarningReview ?? suppliedWarningReview;

  useEffect(() => {
    setManualWarningReview(null);
    setErr('');
  }, [projectId, suppliedCheck]);

  useEffect(() => {
    const openWarningReview = (event) => {
      if (event?.detail?.projectId && event.detail.projectId !== projectId) return;
      if (event?.detail?.warningReview) setManualWarningReview(event.detail.warningReview);
      setOpen(true);
    };
    // 助手栏「查看校验结果」原先只往输入框预填一句话，现在直接展开体检结果
    const openCheck = (event) => {
      if (event?.detail?.projectId && event.detail.projectId !== projectId) return;
      setOpen(true);
    };
    window.addEventListener('mw-package-warning-review', openWarningReview);
    window.addEventListener('mw-package-check-open', openCheck);
    return () => {
      window.removeEventListener('mw-package-warning-review', openWarningReview);
      window.removeEventListener('mw-package-check-open', openCheck);
    };
  }, [projectId]);

  // 两个按钮走同一条路：请求后端在助手里落一张卡片/一条待办，再让工作区刷新把它取回来。
  const handOff = async (finding, mode) => {
    if (handoffId) return;
    setHandoffId(`${finding.id}:${mode}`);
    setErr('');
    try {
      if (mode === 'ignore') await ignorePackageCheckFinding(projectId, finding.id);
      else await reviewPackageCheckFinding(projectId, finding.id);
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (error) {
      setErr(error instanceof Error ? error.message : '交给建模助手失败');
    } finally { setHandoffId(''); }
  };

  const counts = check ? {
    错误: check.findings.filter((f) => f.severity === '错误').length,
    警告: check.findings.filter((f) => f.severity === '警告').length,
    提示: check.findings.filter((f) => f.severity === '提示').length,
  } : null;
  const warningByFindingId = new Map((warningReview?.items ?? []).map((item) => [item.finding?.id, item]));
  const deferred = Boolean(errorDeferral?.valid && errorDeferral.revision === check?.revision);

  const repair = async () => {
    if (repairing) return;
    // 可用性由后端声明：不可用时说明原因，而不是静默什么都不做
    if (repairAvailability && !repairAvailability.available) {
      setErr(repairAvailability.reason || '当前无法自动修复');
      return;
    }
    if (!counts?.错误 || packageDraft) return;
    setRepairing(true); setErr('');
    try {
      await repairAiConstructionKnowledge(projectId);
      window.dispatchEvent(new CustomEvent('mw-package-updated'));
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (error) {
      setErr(error instanceof Error ? error.message.replaceAll('受控修复', '逐项修复') : '修复启动失败');
    } finally { setRepairing(false); }
  };

  return (
    <div className="pcheck">
      <div className="pcheck-bar">
        {rechecking && <span className="tag tag-lang">检查中…</span>}
        {packageDraft && (
          <span className="tag tag-draft">请先发布或放弃人工编辑草稿</span>
        )}
        {counts && (
          <button className="pcheck-sum" onClick={() => setOpen((v) => !v)}>
            v{check.version}：
            <span className="tag tag-err">错误 {counts.错误}</span>
            <span className="tag tag-draft">警告 {counts.警告}</span>
            <span className="tag tag-lang">提示 {counts.提示}</span>
            <span style={{ color: 'var(--muted)' }}>{open ? '收起 ▾' : '展开 ▸'}</span>
          </button>
        )}
        {counts?.错误 > 0 && (
          <button type="button" className="btn btn-p"
            disabled={repairing || rechecking
              || (repairAvailability ? !repairAvailability.available : !!packageDraft)}
            onClick={repair}
            title={repairAvailability && !repairAvailability.available
              ? repairAvailability.reason
              : '根据当前检查结果自动修改知识包，并重新检查'}>
            {repairing ? '自动修复中…' : '自动修复错误'}
          </button>
        )}
        {(err || suppliedError) && <span className="lerr" style={{ margin: 0 }}>{err || suppliedError}</span>}
      </div>
      {open && check && (
        <div className="pcheck-list">
          {!check.findings.length && <div className="empty" style={{ padding: '10px 0' }}>✓ 未发现问题</div>}
          {check.findings.map((f) => {
            const ignorable = f.severity === '错误' || f.severity === '警告';
            const accepted = f.severity === '警告' && warningByFindingId.get(f.id)?.accepted;
            const alreadyIgnored = accepted || (f.severity === '错误' && deferred);
            return (
              <div key={f.id} className="pcheck-item">
                <span className={`tag ${SEV_TAG[f.severity] ?? 'tag-lang'}`}>{f.severity}</span>
                <span className="tag tag-lang">{f.category}</span>
                <button className="pcheck-target" title="定位到该文件" onClick={() => onOpenFile?.(f.target)}>{f.target}</button>
                <div className="pcheck-msg">{f.message}</div>
                {alreadyIgnored && <span className="tag tag-ok">当前内容已忽略</span>}
                <button type="button" className="btn pcheck-act"
                  disabled={Boolean(handoffId)}
                  title="在建模助手里说明这条问题并给出处理方式"
                  onClick={() => { void handOff(f, 'review'); }}>
                  {handoffId === `${f.id}:review` ? '正在打开…' : '去修改'}
                </button>
                {ignorable && !alreadyIgnored && (
                  <button type="button" className="btn pcheck-act"
                    disabled={Boolean(handoffId)}
                    title="在建模助手里说明理由后忽略"
                    onClick={() => { void handOff(f, 'ignore'); }}>
                    {handoffId === `${f.id}:ignore` ? '正在打开…' : '忽略'}
                  </button>
                )}
              </div>
            );
          })}
          {deferred && (
            <div className="pcheck-note">
              当前内容的 {errorDeferral.errorCount} 个错误已记录忽略理由，可继续验证；正式发布仍要求错误清零。
              <span className="muted">忽略说明：{errorDeferral.reason}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
