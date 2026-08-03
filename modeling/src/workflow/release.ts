export type ReleaseGateId = 'package_check' | 'warning_review' | 'flowchart' | 'construction' | 'evaluation';

export interface ReleaseGate {
  id: ReleaseGateId;
  label: string;
  passed: boolean;
  detail: string;
}

export interface ReleaseReadinessInput {
  currentRevision?: string | null;
  packageCheck?: {
    revision?: string;
    findings: Array<{ severity: string }>;
  } | null;
  warningReview?: {
    warningTotal: number;
    pendingValidation: number;
    pendingReleaseConfirmation: number;
    releaseBlocking: number;
    readyForRelease: boolean;
  } | null;
  flowchartConfirmed?: boolean;
  flowchartRequired?: boolean;
  constructionRequired?: boolean;
  constructionCompleted?: boolean;
  pendingRequiredTasks?: number;
  pendingCandidateChanges?: number;
  latestRound?: {
    status: 'running' | 'done' | 'failed';
    artifactRevision?: string;
  } | null;
  requiredTestcases?: Array<{ id: string; question: string; groundTruth: string }>;
  latestResults?: Array<{
    testcaseId?: string;
    question?: string;
    groundTruth?: string;
    verdict?: 'pass' | 'partial' | 'fail' | 'error';
  }>;
}

export interface ReleaseReadiness {
  ready: boolean;
  gates: ReleaseGate[];
}

export function buildReleaseReadiness(input: ReleaseReadinessInput): ReleaseReadiness {
  const checkErrors = input.packageCheck?.findings
    .filter((finding) => finding.severity === '错误').length ?? 0;
  const checkIsCurrent = Boolean(
    input.currentRevision
    && input.packageCheck?.revision
    && input.packageCheck.revision === input.currentRevision,
  );
  const checkPassed = checkIsCurrent && checkErrors === 0;

  const results = input.latestResults ?? [];
  const required = input.requiredTestcases;
  const resultByTestcase = new Map(results.map((result) => [result.testcaseId, result]));
  const matchedResults = required
    ? required.map((testcase) => resultByTestcase.get(testcase.id)).filter(Boolean)
    : results;
  const passCount = matchedResults.filter((result) => result?.verdict === 'pass').length;
  const roundIsCurrent = Boolean(
    input.currentRevision
    && input.latestRound?.artifactRevision
    && input.latestRound.artifactRevision === input.currentRevision,
  );
  const scopeMatches = required
    ? required.length > 0
      && results.length === required.length
      && required.every((testcase) => {
        const result = resultByTestcase.get(testcase.id);
        return result?.question === testcase.question
          && result?.groundTruth === testcase.groundTruth;
      })
    : results.length > 0;
  const evaluationPassed = input.latestRound?.status === 'done'
    && roundIsCurrent
    && scopeMatches;
  const evaluatedCount = required?.length ?? results.length;
  const nonPassCount = Math.max(0, evaluatedCount - passCount);

  const pendingRequiredTasks = Math.max(0, input.pendingRequiredTasks ?? 0);
  const pendingCandidateChanges = Math.max(0, input.pendingCandidateChanges ?? 0);
  const constructionPassed = input.constructionCompleted === true
    && pendingRequiredTasks === 0
    && pendingCandidateChanges === 0;

  const gates: ReleaseGate[] = [
    {
      id: 'package_check',
      label: '内容检查',
      passed: checkPassed,
      detail: checkPassed
        ? '当前版本已通过'
        : !input.packageCheck
          ? '尚未检查'
          : !checkIsCurrent
            ? '内容有改动，请重查'
            : `${checkErrors} 个错误`,
    },
    {
      id: 'warning_review',
      label: '警告与已知限制',
      passed: input.warningReview?.readyForRelease === true,
      detail: input.warningReview?.readyForRelease === true
        ? input.warningReview.warningTotal > 0
          ? `${input.warningReview.warningTotal} 条可暂缓的提醒已确认为已知限制`
          : '当前修订没有警告'
        : !input.warningReview
          ? '尚未检查当前修订警告'
          : input.warningReview.releaseBlocking > 0
            ? `${input.warningReview.releaseBlocking} 条发布阻断警告必须修复`
            : input.warningReview.pendingValidation > 0
              ? `${input.warningReview.pendingValidation} 条警告尚未在验证阶段接受`
              : `${input.warningReview.pendingReleaseConfirmation} 条警告尚未确认为发布已知限制`,
    },
    ...(input.flowchartRequired === false ? [] : [{
      id: 'flowchart' as const,
      label: '流程图确认',
      passed: input.flowchartConfirmed === true,
      detail: input.flowchartConfirmed === true ? '专家已确认' : '尚未确认',
    }]),
    ...(input.constructionRequired === true ? [{
      id: 'construction' as const,
      label: '建模完成确认',
      passed: constructionPassed,
      detail: constructionPassed
        ? '当前建模基线已确认'
        : input.constructionCompleted !== true
          ? 'AI 辅助构建尚未确认完成'
          : pendingRequiredTasks > 0
            ? `还有 ${pendingRequiredTasks} 个必选任务未完成`
            : `还有 ${pendingCandidateChanges} 个候选修改待处理`,
    }] : []),
    {
      id: 'evaluation',
      label: '评测',
      passed: evaluationPassed,
      detail: evaluationPassed
        ? nonPassCount > 0
          ? `${evaluatedCount}/${evaluatedCount} 已完成，其中 ${passCount} 条通过、${nonPassCount} 条待改进（不阻断发布）`
          : `${passCount}/${evaluatedCount} 通过`
        : !input.latestRound
          ? '尚未评测'
          : input.latestRound.status !== 'done'
            ? '尚未完成'
            : !input.currentRevision || !input.latestRound.artifactRevision
              ? '评测未记录工件修订，请对当前知识包重新评测'
              : !roundIsCurrent
                ? '评测使用的是旧修订，请对当前知识包重新评测'
              : !scopeMatches
                ? `${matchedResults.length}/${required?.length ?? results.length} 条当前真实用例已评测`
                : `${passCount}/${required?.length ?? results.length} 通过`,
    },
  ];

  return { ready: gates.every((gate) => gate.passed), gates };
}
