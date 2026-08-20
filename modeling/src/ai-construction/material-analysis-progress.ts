import type { AiConstructionMessageBlock } from './messages.js';

export interface MaterialAnalysisProgress {
  phase: string;
  title: string;
  summary?: string;
  percent: number;
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function elapsedText(elapsedMs: number): string {
  const seconds = Math.max(0, Math.round(elapsedMs / 1000));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
}

function remainingEta(percent: number, elapsedMs: number) {
  if (percent >= 100) {
    return { minMinutes: 0, maxMinutes: 0, confidence: 'high' as const };
  }
  const fraction = Math.max(0.01, percent / 100);
  const observedTotalSeconds = (Math.max(1, elapsedMs) / 1000) / fraction;
  const estimatedTotalSeconds = percent < 10
    ? 300
    : Math.max(120, Math.min(14_400, observedTotalSeconds));
  const remainingSeconds = estimatedTotalSeconds * (1 - fraction);
  return {
    minMinutes: Math.max(1, Math.ceil((remainingSeconds * 0.75) / 60)),
    maxMinutes: Math.max(1, Math.ceil((remainingSeconds * 1.35) / 60)),
    confidence: percent >= 70 ? 'high' as const : percent >= 30 ? 'medium' as const : 'low' as const,
  };
}

export function buildMaterialAnalysisProgressBlocks(
  input: MaterialAnalysisProgress,
  elapsedMs: number,
): AiConstructionMessageBlock[] {
  const percent = clampPercent(input.percent);
  const eta = remainingEta(percent, elapsedMs);
  const detail = percent >= 100
    ? `已用时约 ${elapsedText(elapsedMs)}，分析阶段已完成。`
    : `已用时约 ${elapsedText(elapsedMs)}，剩余约 ${eta.minMinutes}–${eta.maxMinutes} 分钟。`;
  return [
    {
      type: 'status',
      title: input.title,
      ...(input.summary ? { summary: input.summary } : {}),
      progress: percent,
      eta,
    },
    { type: 'text', text: detail },
  ];
}

export function buildMaterialAnalysisFailureBlocks(input: {
  reason: string;
  milestoneId: string;
  taskId: string;
  progress: number;
}): AiConstructionMessageBlock[] {
  return [
    {
      type: 'status',
      title: '建模资料分析已停止',
      summary: '当前进度已保留，可以直接重试',
      milestoneId: input.milestoneId,
      taskId: input.taskId,
      progress: clampPercent(input.progress),
      eta: { minMinutes: null, maxMinutes: null, confidence: 'low' },
    },
    { type: 'text', text: input.reason.trim().slice(0, 3_000) || '资料分析意外中断，请稍后重试。' },
    {
      type: 'next_actions',
      title: '可以这样继续',
      actions: [
        { id: 'analyze_materials', label: '重新分析资料', style: 'primary' },
        { id: 'upload_materials', label: '补充建模资料', style: 'secondary' },
      ],
    },
  ];
}
