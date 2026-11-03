import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Modal, Space, Spin, Tooltip, Typography, message } from 'antd';
import { api } from '../lib/api';
import { POLL_INTERVAL_MS, RECONCILE_EVERY, mergeStatus, needsPolling } from '../lib/packageEvaluation.js';
import { Markdown } from './CenterPane.jsx';

const REPORT_LIMIT_HINT = '报告较长，仅显示前 2 MiB';
const REPORT_OPTIMIZE_HINT = '此处仅查看评测结果。如需据此优化知识包，请打开「优化建议」并点击「评测优化」。';
const EMPTY_PATHS = new Set();
// 报告与建议篇幅长：弹窗尽量放大，同时在视口左右和上下各留出边距
const MODAL_WIDTH = 'min(1280px, calc(100vw - 96px))';
const bodyStyle = { maxHeight: 'calc(100vh - 200px)', overflow: 'auto', padding: '0 12px' };

// 报告内容读自评测服务：完整回答附件要等评测优化导入后才在工作区，文件链接也不在弹窗内跳转
const showAnswerHint = () => { message.info('完整回答会在执行评测优化后写入工作区 reports/ 目录'); return true; };
const showFileHint = (path) => message.info(`请在建模工作区中查看 ${path}`);

// 打开时向评测服务对账一次；仍有评测或产物在进行时轮询本地索引，并定期再对账
export function usePackageEvaluations(projectId, revision) {
  const [status, setStatus] = useState(null);
  const [refreshes, setRefreshes] = useState(0);
  const pending = useRef(false);

  useEffect(() => { setStatus(null); }, [projectId]);
  useEffect(() => {
    if (!projectId) return undefined;
    let alive = true;
    api.packageEvaluations(projectId, { reconcile: true })
      .then((result) => { if (alive) setStatus(result); })
      .catch((e) => { if (alive) setStatus({ configured: false, message: `评测状态读取失败：${e.message}`, releases: {} }); });
    return () => { alive = false; };
  }, [projectId, revision, refreshes]);

  const polling = !!projectId && needsPolling(status);
  useEffect(() => {
    if (!polling) return undefined;
    let alive = true;
    let count = 0;
    const timer = setInterval(() => {
      if (pending.current) return;
      count += 1;
      pending.current = true;
      api.packageEvaluations(projectId, { reconcile: count % RECONCILE_EVERY === 0 })
        .then((result) => { if (alive) setStatus((previous) => mergeStatus(previous, result)); })
        .catch(() => {})
        .finally(() => { pending.current = false; });
    }, POLL_INTERVAL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, [projectId, polling]);

  return { status, refresh: () => setRefreshes((n) => n + 1) };
}

// 禁用的按钮不触发悬停事件：外包 span 才能显示置灰原因；tooltip 传给提示框（位置、偏移等）
export function ReasonButton({ reason, tooltip, children, ...props }) {
  const button = <Button disabled={!!reason} {...props}>{children}</Button>;
  return reason ? <Tooltip title={reason} {...tooltip}><span>{button}</span></Tooltip> : button;
}

function useArtifact(load, target) {
  const [state, setState] = useState({ loading: false, error: '', data: null });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!target) return undefined;
    let alive = true;
    setState({ loading: true, error: '', data: null });
    load(target)
      .then((data) => { if (alive) setState({ loading: false, error: '', data }); })
      .catch((e) => { if (alive) setState({ loading: false, error: e.message, data: null }); });
    return () => { alive = false; };
  }, [target, revision]);
  return { ...state, retry: () => setRevision((n) => n + 1) };
}

function ArtifactBody({ loading, error, retry, data }) {
  if (loading) return <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>;
  if (error) {
    return <Alert type="error" showIcon message={error}
      action={<Button size="small" onClick={retry}>重试</Button>} />;
  }
  if (!data?.content) return null;
  return (
    <div style={bodyStyle}>
      <Markdown content={data.content} knownPaths={EMPTY_PATHS} onOpenFile={showFileHint} onOpenAnswer={showAnswerHint} />
    </div>
  );
}

// target: { runId, version }
export function EvaluationReportModal({ projectId, target, onClose }) {
  const artifact = useArtifact((t) => api.packageEvaluationReport(projectId, t.runId), target);
  return (
    <Modal title={`评测报告 — 版本 ${target?.version || ''}`} open={!!target} onCancel={onClose} width={MODAL_WIDTH} centered
      footer={(
        <Space>
          <Typography.Text type="secondary">{REPORT_OPTIMIZE_HINT}</Typography.Text>
          <Button onClick={onClose}>关闭</Button>
        </Space>
      )} destroyOnHidden>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {artifact.data?.truncated && <Alert type="info" showIcon message={REPORT_LIMIT_HINT} />}
        <ArtifactBody {...artifact} />
      </Space>
    </Modal>
  );
}

// target: { runId, version }；blockReason 非空时评测优化置灰
export function EvaluationSuggestionsModal({ projectId, target, blockReason, onClose, onRegenerated, onOptimized }) {
  const artifact = useArtifact((t) => api.packageEvaluationSuggestions(projectId, t.runId), target);
  const [regenerating, setRegenerating] = useState(false);
  const [optimizing, setOptimizing] = useState(false);
  const failed = artifact.data?.status === 'failed';

  const regenerate = async () => {
    setRegenerating(true);
    try {
      await api.regeneratePackageEvaluation(projectId, target.runId);
      message.success('已开始重新生成评测报告和优化建议');
      onRegenerated?.();
    } catch (e) {
      message.error(e.message);
    } finally {
      setRegenerating(false);
    }
  };

  const optimize = async () => {
    setOptimizing(true);
    try {
      const result = await api.optimizePackageEvaluation(projectId, target.runId);
      message.success('已导入评测报告和优化建议，开始评测优化');
      onOptimized?.(result.command);
    } catch (e) {
      message.error(e.message);
    } finally {
      setOptimizing(false);
    }
  };

  return (
    <Modal title={`优化建议 — 版本 ${target?.version || ''}`} open={!!target} onCancel={onClose} width={MODAL_WIDTH} centered
      destroyOnHidden
      footer={(
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <Button onClick={onClose}>关闭</Button>
          <ReasonButton type="primary" reason={blockReason} loading={optimizing} onClick={optimize}>评测优化</ReasonButton>
        </div>
      )}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {failed && (
          <Alert type="error" showIcon message="优化建议生成失败" description={artifact.data.error}
            action={<Button size="small" loading={regenerating} onClick={regenerate}>重新生成</Button>} />
        )}
        <ArtifactBody {...artifact} />
      </Space>
    </Modal>
  );
}
