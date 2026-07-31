import { useEffect, useState } from 'react';
import {
  acceptPackageWarnings,
  deferPackageValidationErrors,
  runPackageCheck,
} from './api.js';
import { repairAiConstructionKnowledge } from './authoring-api.js';
import { BusyButton } from './ui.jsx';

// 知识包体检面板（知识包完善页顶部）：规则引擎 + 语义 agent 的结构化发现。
// 每条发现可一键把修复指令预填进右栏修缮对话。
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
  rechecking = false,
  error: suppliedError = '',
  onCheckResult,
}) {
  const [manualCheck, setManualCheck] = useState(null);
  const [manualErrorDeferral, setManualErrorDeferral] = useState(null);
  const [manualWarningReview, setManualWarningReview] = useState(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [acceptanceNote, setAcceptanceNote] = useState('');
  const [accepting, setAccepting] = useState(false);
  const [deferralReason, setDeferralReason] = useState('');
  const [deferring, setDeferring] = useState(false);
  const [repairing, setRepairing] = useState(false);

  // 手动检查与审计操作先本地生效，等上层快照回流后再让位，避免闪回旧结果。
  const check = manualCheck ?? suppliedCheck;
  const errorDeferral = manualErrorDeferral ?? suppliedErrorDeferral;
  const warningReview = manualWarningReview ?? suppliedWarningReview;

  useEffect(() => {
    setManualCheck(null);
    setManualErrorDeferral(null);
    setManualWarningReview(null);
    setAcceptanceNote('');
    setDeferralReason('');
  }, [projectId, suppliedCheck]);

  useEffect(() => {
    const openWarningReview = (event) => {
      if (event?.detail?.projectId && event.detail.projectId !== projectId) return;
      if (event?.detail?.warningReview) setManualWarningReview(event.detail.warningReview);
      setOpen(true);
    };
    window.addEventListener('mw-package-warning-review', openWarningReview);
    return () => window.removeEventListener('mw-package-warning-review', openWarningReview);
  }, [projectId]);

  const run = async () => {
    if (packageDraft) {
      setErr('请先发布或放弃人工编辑草稿，再运行知识包检查');
      return;
    }
    setBusy(true); setErr('');
    try {
      const d = await runPackageCheck(projectId);
      setManualCheck({
        version: d.version, revision: d.revision, findings: d.findings,
        createdAt: new Date().toISOString(),
      });
      setManualErrorDeferral(d.errorDeferral);
      setManualWarningReview(d.warningReview);
      setAcceptanceNote('');
      setDeferralReason('');
      setOpen(true);
      onCheckResult?.(d);
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const toChat = (f) => {
    // 预填带全上下文（类别/目标/现象/修复要求）——只发 suggestion 一句话 agent 不知道改哪个问题
    const text = [
      `修复体检发现（${f.category} · ${f.target}）：${f.message}`,
      f.suggestion ? `修复要求：${f.suggestion}` : '',
    ].filter(Boolean).join('\n');
    window.dispatchEvent(new CustomEvent('mw-chat-prefill', { detail: text }));
  };

  const counts = check ? {
    错误: check.findings.filter((f) => f.severity === '错误').length,
    警告: check.findings.filter((f) => f.severity === '警告').length,
    提示: check.findings.filter((f) => f.severity === '提示').length,
  } : null;
  const warningByFindingId = new Map((warningReview?.items ?? []).map((item) => [item.finding?.id, item]));
  const pendingWarnings = (warningReview?.items ?? []).filter((item) => !item.accepted);

  const acceptWarnings = async () => {
    if (!check?.revision || !pendingWarnings.length || accepting) return;
    setAccepting(true); setErr('');
    try {
      const result = await acceptPackageWarnings(
        projectId, check.revision, pendingWarnings.map((item) => item.fingerprint), acceptanceNote, 'validation',
      );
      setManualWarningReview(result.warningReview);
      setAcceptanceNote('');
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (error) {
      setErr(error instanceof Error ? error.message : '警告确认失败');
    } finally { setAccepting(false); }
  };

  const deferErrors = async () => {
    if (!check?.revision || !counts?.错误 || deferring) return;
    setDeferring(true); setErr('');
    try {
      const result = await deferPackageValidationErrors(projectId, deferralReason);
      setManualErrorDeferral(result.deferral);
      setDeferralReason('');
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (error) {
      setErr(error instanceof Error ? error.message : '错误暂缓失败');
    } finally { setDeferring(false); }
  };

  const repair = async () => {
    if (repairing || !counts?.错误 || packageDraft) return;
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
        <BusyButton busy={busy || rechecking} busyText="检查中" expect="约 1~3 分钟" className="btn"
          disabled={!!packageDraft} onClick={run}>
          检查内容
        </BusyButton>
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
            disabled={repairing || busy || rechecking || !!packageDraft} onClick={repair}
            title="根据当前检查结果自动修改知识包，并重新检查">
            {repairing ? '自动修复中…' : '自动修复错误'}
          </button>
        )}
        {!counts && !busy && !rechecking && <span style={{ fontSize: 12, color: 'var(--muted)' }}>检查内容和引用</span>}
        {(err || suppliedError) && <span className="lerr" style={{ margin: 0 }}>{err || suppliedError}</span>}
      </div>
      {open && check && (
        <div className="pcheck-list">
          {!check.findings.length && <div className="empty" style={{ padding: '10px 0' }}>✓ 未发现问题</div>}
          {check.findings.map((f) => (
            <div key={f.id} className="pcheck-item">
              <span className={`tag ${SEV_TAG[f.severity] ?? 'tag-lang'}`}>{f.severity}</span>
              <span className="tag tag-lang">{f.category}</span>
              <button className="pcheck-target" title="定位到该文件" onClick={() => onOpenFile?.(f.target)}>{f.target}</button>
              <div className="pcheck-msg">{f.message}</div>
              {f.severity === '警告' && warningByFindingId.get(f.id)?.accepted && (
                <span className="tag tag-ok">当前修订已接受</span>
              )}
              {f.suggestion && (
                <button className="btn" style={{ padding: '2px 10px', fontSize: 11.5, flex: 'none' }} onClick={() => toChat(f)}>
                  去修改
                </button>
              )}
            </div>
          ))}
          {counts.错误 > 0 && (
            <div className="pcheck-warning-acceptance">
              <strong>暂缓阻断错误进入验证</strong>
              {errorDeferral?.valid && errorDeferral.revision === check.revision ? (
                <>
                  <p>当前修订的 {errorDeferral.errorCount} 个错误已记录暂缓原因，可继续验证；正式发布仍要求错误清零。</p>
                  <p className="muted">暂缓说明：{errorDeferral.reason}</p>
                </>
              ) : (
                <>
                  <p>仅允许当前错误集合先进入验证闭环。内容或错误变化后记录自动失效，正式发布仍会阻断。</p>
                  <textarea
                    rows={2}
                    value={deferralReason}
                    placeholder="说明暂缓原因和后续处理计划（必填）"
                    onChange={(event) => setDeferralReason(event.target.value)}
                  />
                  <button
                    type="button"
                    className="btn"
                    disabled={deferring || deferralReason.trim().length < 4}
                    onClick={() => { void deferErrors(); }}
                  >
                    {deferring ? '记录中…' : `暂缓 ${counts.错误} 个错误`}
                  </button>
                </>
              )}
            </div>
          )}
          {pendingWarnings.length > 0 && (
            <div className="pcheck-warning-acceptance">
              <strong>接受非阻断警告进入验证</strong>
              <p>请确认已查看这 {pendingWarnings.length} 条警告的影响。接受记录只对当前知识包修订有效。</p>
              <textarea
                rows={2}
                value={acceptanceNote}
                placeholder="说明影响范围和接受理由（必填）"
                onChange={(event) => setAcceptanceNote(event.target.value)}
              />
              <button
                type="button"
                className="btn"
                disabled={accepting || acceptanceNote.trim().length < 4}
                onClick={() => { void acceptWarnings(); }}
              >
                {accepting ? '确认中…' : `接受 ${pendingWarnings.length} 条警告`}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
