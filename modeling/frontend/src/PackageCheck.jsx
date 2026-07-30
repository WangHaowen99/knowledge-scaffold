import { useEffect, useState } from 'react';
import {
  acceptPackageWarnings,
  deferPackageValidationErrors,
  runPackageCheck,
  getPackageCheck,
} from './api.js';
import { BusyButton } from './ui.jsx';

// 知识包体检面板（知识包完善页顶部）：规则引擎 + 语义 agent 的结构化发现。
// 每条发现可一键把修复指令预填进右栏修缮对话。
const SEV_TAG = { 错误: 'tag-err', 警告: 'tag-draft', 提示: 'tag-lang' };

export default function PackageCheck({ projectId, onOpenFile, packageDraft }) {
  const [check, setCheck] = useState(null);   // { version, findings, createdAt } | null
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [warningReview, setWarningReview] = useState(null);
  const [acceptanceNote, setAcceptanceNote] = useState('');
  const [accepting, setAccepting] = useState(false);
  const [errorDeferral, setErrorDeferral] = useState(null);
  const [deferralReason, setDeferralReason] = useState('');
  const [deferring, setDeferring] = useState(false);

  useEffect(() => {
    getPackageCheck(projectId).then((d) => {
      setCheck(d.check);
      setErrorDeferral(d.errorDeferral);
      setWarningReview(d.warningReview);
    }).catch(() => {});
  }, [projectId]);

  const run = async () => {
    if (packageDraft) return;
    setBusy(true); setErr('');
    try {
      const d = await runPackageCheck(projectId);
      setCheck({
        version: d.version,
        revision: d.revision,
        findings: d.findings,
        createdAt: new Date().toISOString(),
      });
      setErrorDeferral(d.errorDeferral);
      setWarningReview(d.warningReview);
      setAcceptanceNote('');
      setDeferralReason('');
      setOpen(true);
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
    if (!check?.revision || !pendingWarnings.length || accepting || packageDraft) return;
    setAccepting(true);
    setErr('');
    try {
      const result = await acceptPackageWarnings(
        projectId,
        check.revision,
        pendingWarnings.map((item) => item.fingerprint),
        acceptanceNote,
        'validation',
      );
      setWarningReview(result.warningReview);
      setAcceptanceNote('');
    } catch (error) {
      setErr(error instanceof Error ? error.message : '警告确认失败');
    } finally {
      setAccepting(false);
    }
  };

  const deferErrors = async () => {
    if (!check?.revision || !counts?.错误 || deferring || packageDraft) return;
    setDeferring(true);
    setErr('');
    try {
      const result = await deferPackageValidationErrors(projectId, deferralReason);
      setErrorDeferral(result.deferral);
      setDeferralReason('');
    } catch (error) {
      setErr(error instanceof Error ? error.message : '错误暂缓失败');
    } finally {
      setDeferring(false);
    }
  };

  return (
    <div className="pcheck">
      <div className="pcheck-bar">
        <BusyButton busy={busy} busyText="体检中" expect="含语义检查约 1~3 分钟" className="btn"
          disabled={!!packageDraft} title={packageDraft ? '请先发布或放弃人工编辑草稿' : ''} onClick={run}>
          🩺 知识包体检
        </BusyButton>
        {counts && (
          <button className="pcheck-sum" onClick={() => setOpen((v) => !v)}>
            v{check.version}：
            <span className="tag tag-err">错误 {counts.错误}</span>
            <span className="tag tag-draft">警告 {counts.警告}</span>
            <span className="tag tag-lang">提示 {counts.提示}</span>
            <span style={{ color: 'var(--muted)' }}>{open ? '收起 ▾' : '展开 ▸'}</span>
          </button>
        )}
        {packageDraft
          ? <span className="draft-inline-gate">请先发布或放弃人工编辑草稿，再运行知识包体检</span>
          : !counts && !busy && <span style={{ fontSize: 12, color: 'var(--muted)' }}>检查口径冲突、兜底缺失、路由重复等规格问题</span>}
        {err && <span className="lerr" style={{ margin: 0 }}>{err}</span>}
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
                <button className="btn" style={{ padding: '2px 10px', fontSize: 11.5, flex: 'none' }}
                  disabled={!!packageDraft} title={packageDraft ? '请先发布或放弃人工编辑草稿' : ''} onClick={() => toChat(f)}>
                  交给修缮对话 →
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
                    disabled={!!packageDraft}
                    onChange={(event) => setDeferralReason(event.target.value)}
                  />
                  <button
                    type="button"
                    className="btn"
                    disabled={deferring || !!packageDraft || deferralReason.trim().length < 4}
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
                disabled={!!packageDraft}
                onChange={(event) => setAcceptanceNote(event.target.value)}
              />
              <button
                type="button"
                className="btn"
                disabled={accepting || !!packageDraft || acceptanceNote.trim().length < 4}
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
