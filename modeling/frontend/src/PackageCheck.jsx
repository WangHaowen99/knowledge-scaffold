import { useEffect, useState } from 'react';
import {
  ignorePackageCheckFinding,
  repairAiConstructionKnowledge,
  reviewPackageCheckFinding,
} from './authoring-api.js';
import { acceptPackageWarnings } from './api.js';
import {
  currentFindingIgnoreInteraction,
  findingIgnoreInteraction,
} from './checkFindingHandoff.js';

// 知识包体检面板（知识包完善页顶部）：格式和规则引擎的结构化发现。
//
// 这里只负责「列出发现」。每条发现的处理——修改和暂缓——都交给建模助手：
// 暂缓要写审计记录、要填理由，在面板里就地填表既和助手里的处理路径重复，
// 刷新还会丢；助手那边已经有卡片和待办两套现成的承载方式。
//
// 自动重查由 PackageWorkspace 统一驱动并经 props 下发：这个面板只在「编辑与检查」页签下
// 挂载，如果重查跟着它走，评测和诊断页签下改包就不会自动校验。
const SEV_TAG = { 错误: 'tag-err', 警告: 'tag-draft', 提示: 'tag-lang' };
const DISPOSITION_LABEL = {
  automatic: '结构修复',
  approval_required: '需要确认',
  expert_decision: '专家决策',
  external_data_required: '资料或数据缺口',
  retryable_infrastructure: '服务可恢复',
};
const DISPOSITION_ACTION = {
  automatic: '自动修复本项',
  approval_required: '查看确认方案',
  expert_decision: '回答业务问题',
  external_data_required: '补充资料或数据',
  retryable_infrastructure: '恢复当前步骤',
};

export default function PackageCheck({
  projectId,
  onOpenFile,
  packageDraft = null,
  check: suppliedCheck = null,
  warningReview: suppliedWarningReview = null,
  errorDeferral: suppliedErrorDeferral = null,
  repair: repairAvailability = null,
  packageRepair = null,
  rechecking = false,
  error: suppliedError = '',
  pendingInteractions = [],
  packageRevision = '',
  onRecheck,
  onInteractionOpened,
  onInteractionFocus,
}) {
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState('');
  const [handoffId, setHandoffId] = useState('');
  const [repairing, setRepairing] = useState(false);
  const [selectedWarnings, setSelectedWarnings] = useState([]);
  const [warningNote, setWarningNote] = useState('');
  const [warningBusy, setWarningBusy] = useState(false);

  // 助手侧的处理结果回流前，先本地生效，避免闪回旧结果
  const check = suppliedCheck;
  const errorDeferral = suppliedErrorDeferral;
  useEffect(() => {
    setErr('');
  }, [projectId, suppliedCheck]);

  useEffect(() => {
    setSelectedWarnings([]);
    setWarningNote('');
  }, [projectId, suppliedWarningReview?.revision]);

  useEffect(() => {
    const openWarningReview = (event) => {
      if (event?.detail?.projectId && event.detail.projectId !== projectId) return;
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

  // 两个按钮走同一条路：请求后端在助手里落一张卡片/一条待办；暂缓表单会立即
  // 注入当前会话，后续刷新只负责与服务端最终状态对账。
  const handOff = async (finding, mode) => {
    if (handoffId) return;
    setHandoffId(`${finding.id}:${mode}`);
    setErr('');
    try {
      if (mode === 'ignore') {
        const result = await ignorePackageCheckFinding(projectId, finding.id);
        if (!result?.interaction) throw new Error('服务端未返回暂缓理由表单，请重试');
        onInteractionOpened?.(result);
      } else {
        await reviewPackageCheckFinding(projectId, finding.id);
      }
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (error) {
      setErr(error instanceof Error ? error.message : '交给建模助手失败');
    } finally { setHandoffId(''); }
  };

  const blockingFindings = check?.findings.filter((f) => f.severity === '错误') ?? [];
  const pendingWarnings = (suppliedWarningReview?.items ?? []).filter((item) => !item.accepted);
  const counts = check ? {
    错误: blockingFindings.length,
    警告: check.findings.filter((finding) => finding.severity === '警告').length,
    提示: check.findings.filter((finding) => finding.severity === '提示').length,
  } : null;
  const deferred = Boolean(errorDeferral?.valid && errorDeferral.revision === check?.revision);
  const currentRevision = packageRevision || check?.revision;
  const pendingIgnore = currentFindingIgnoreInteraction(pendingInteractions, currentRevision);

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

  const toggleWarning = (fingerprint) => {
    setSelectedWarnings((current) => current.includes(fingerprint)
      ? current.filter((item) => item !== fingerprint)
      : [...current, fingerprint]);
  };

  const acceptWarnings = async () => {
    if (warningBusy || !suppliedWarningReview?.revision || !selectedWarnings.length) return;
    setWarningBusy(true); setErr('');
    try {
      await acceptPackageWarnings(
        projectId,
        suppliedWarningReview.revision,
        selectedWarnings,
        warningNote,
        'validation',
      );
      window.dispatchEvent(new CustomEvent('mw-package-check-updated'));
    } catch (error) {
      setErr(error instanceof Error ? error.message : '警告复核失败');
    } finally { setWarningBusy(false); }
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
            {counts.错误 > 0 && <span className="tag tag-err">发布阻断</span>}
            <span style={{ color: 'var(--muted)' }}>{open ? '收起 ▾' : '展开 ▸'}</span>
          </button>
        )}
        <button type="button" className="btn"
          disabled={!onRecheck || rechecking || repairing || Boolean(handoffId) || Boolean(packageDraft)}
          title={packageDraft
            ? '请先发布或放弃人工编辑草稿，再重新检查正式知识包'
            : '对当前知识包重新执行格式、引用、来源映射和规则检查'}
          onClick={() => { void onRecheck?.(); }}>
          {(() => { if (rechecking) return '检查中…'; return check ? '重新检查' : '执行检查'; })()}
        </button>
        {counts?.错误 > 0 && (
          <button type="button" className="btn btn-p"
            disabled={repairing || rechecking
              || (repairAvailability ? !repairAvailability.available : !!packageDraft)}
            onClick={repair}
            title={repairAvailability && !repairAvailability.available
              ? repairAvailability.reason
              : '逐项分类处理当前知识包错误，并在候选通过复检后应用'}>
            {repairing ? '正在启动处理…' : '逐项处理错误'}
          </button>
        )}
        {(err || suppliedError) && <span className="lerr" style={{ margin: 0 }}>{err || suppliedError}</span>}
      </div>
      {!blockingFindings.length && pendingWarnings.length > 0 && (
        <section className="pcheck-warning-acceptance" aria-label="警告复核">
          <div className="pcheck-warning-head">
            <div>
              <strong>复核 {pendingWarnings.length} 条警告</strong>
              <p>警告不会阻止知识结构确认；仍应先判断是否需要修改，确认不影响本轮目标后才能接受。</p>
            </div>
            <label>
              <input type="checkbox"
                checked={selectedWarnings.length === pendingWarnings.length}
                onChange={() => setSelectedWarnings(
                  selectedWarnings.length === pendingWarnings.length
                    ? []
                    : pendingWarnings.map((item) => item.fingerprint),
                )} />
              全选
            </label>
          </div>
          <div className="pcheck-warning-list">
            {pendingWarnings.map((item) => (
              <label key={item.fingerprint} className="pcheck-warning-item">
                <input type="checkbox"
                  checked={selectedWarnings.includes(item.fingerprint)}
                  onChange={() => toggleWarning(item.fingerprint)} />
                <span>
                  <strong>{item.finding.category}</strong>
                  <button type="button" className="pcheck-target"
                    onClick={(event) => {
                      event.preventDefault();
                      onOpenFile?.(item.finding.target);
                    }}>{item.finding.target}</button>
                  <span>{item.finding.message}</span>
                </span>
              </label>
            ))}
          </div>
          <textarea rows={2} value={warningNote}
            placeholder="说明所选警告为什么不影响本轮建模结论（必填）"
            onChange={(event) => setWarningNote(event.target.value)} />
          <button type="button" className="btn"
            disabled={warningBusy || selectedWarnings.length === 0 || warningNote.trim().length < 4}
            onClick={() => { void acceptWarnings(); }}>
            {warningBusy ? '提交中…' : `接受 ${selectedWarnings.length} 条警告`}
          </button>
        </section>
      )}
      {open && check && (
        <div className="pcheck-list">
          {!blockingFindings.length && <div className="empty" style={{ padding: '10px 0' }}>✓ 未发现需要处理的错误</div>}
          {blockingFindings.map((f) => {
            const repairFinding = packageRepair?.findings?.find((item) => item.id === f.id);
            const disposition = repairFinding?.disposition ?? 'approval_required';
            const ignorable = true;
            const accepted = false;
            const alreadyIgnored = accepted || (f.severity === '错误' && deferred);
            const openedIgnore = findingIgnoreInteraction(
              pendingInteractions,
              f,
              currentRevision,
            );
            const anotherIgnoreIsOpen = Boolean(pendingIgnore && !openedIgnore);
            return (
              <div key={f.id} className="pcheck-item">
                <span className={`tag ${SEV_TAG[f.severity] ?? 'tag-lang'}`}>{f.severity}</span>
                <span className="tag tag-lang">{f.category}</span>
                <span className="tag tag-lang">{DISPOSITION_LABEL[disposition] ?? '需要处理'}</span>
                <button className="pcheck-target" title="定位到该文件" onClick={() => onOpenFile?.(f.target)}>{f.target}</button>
                <div className="pcheck-msg">{f.message}</div>
                {repairFinding?.reason && <div className="pcheck-repair-reason">{repairFinding.reason}</div>}
                {repairFinding?.impact && <div className="pcheck-repair-impact">影响：{repairFinding.impact}</div>}
                {repairFinding?.facts && Object.keys(repairFinding.facts).length > 0 && (
                  <dl className="pcheck-facts">
                    {Object.entries(repairFinding.facts).slice(0, 6).map(([key, value]) => (
                      <div key={key}><dt>{key}</dt><dd>{Array.isArray(value) ? value.join('、') : String(value)}</dd></div>
                    ))}
                  </dl>
                )}
                {alreadyIgnored && <span className="tag tag-ok">当前内容已暂缓</span>}
                <button type="button" className="btn pcheck-act"
                  disabled={Boolean(handoffId)}
                  title="在建模助手里说明这条问题并给出处理方式"
                  onClick={() => { void handOff(f, 'review'); }}>
                  {handoffId === `${f.id}:review`
                    ? '正在打开…'
                    : DISPOSITION_ACTION[disposition] ?? '去处理'}
                </button>
                {ignorable && !alreadyIgnored && (
                  <button type="button" className="btn pcheck-act"
                    disabled={Boolean(handoffId) || anotherIgnoreIsOpen}
                    title={(() => { if (anotherIgnoreIsOpen) return '请先在建模助手中完成当前暂缓理由表单'; return openedIgnore
                      ? '暂缓理由表单已在建模助手中打开，点击前往处理'
                      : '在建模助手里说明理由后暂缓；发布前仍需清零'; })()}
                    onClick={() => {
                      if (openedIgnore) onInteractionFocus?.(openedIgnore);
                      else void handOff(f, 'ignore');
                    }}>
                    {(() => { if (handoffId === `${f.id}:ignore`) return '正在打开…'; return openedIgnore ? '前往助手处理' : '暂缓并说明理由'; })()}
                  </button>
                )}
              </div>
            );
          })}
          {deferred && (
            <div className="pcheck-note">
              当前内容的 {errorDeferral.errorCount} 个错误已记录暂缓理由，可继续验证；正式发布仍要求错误清零。
              <span className="muted">暂缓说明：{errorDeferral.reason}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
