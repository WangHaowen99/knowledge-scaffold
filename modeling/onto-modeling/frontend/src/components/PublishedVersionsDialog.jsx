import { useEffect, useState } from 'react';
import { Alert, Button, Empty, Input, Modal, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import { api } from '../lib/api';
import { openEvaluation, prefetchEvaluationEntry } from '../lib/evaluation.js';
import {
  EVALUATION_HINT, SOLVER_VALIDATION_STATES, canReportFailure, hasFeedback, optimizationBlock, processingState, releaseEvaluation,
  releaseFeedback, releaseSolverValidation, writebackNotice,
} from '../lib/packageEvaluation.js';
import { EvaluationReportModal, EvaluationSuggestionsModal, ReasonButton, usePackageEvaluations } from './PackageEvaluationModals.jsx';

const LINK = { type: 'link', size: 'small', style: { padding: 0 } };

export default function PublishedVersionsDialog({ projectId, onClose, onCommand }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ releases: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [downloading, setDownloading] = useState('');
  const [resources, setResources] = useState(null);
  const [reportTarget, setReportTarget] = useState(null);
  const [suggestionsTarget, setSuggestionsTarget] = useState(null);
  const [failureTarget, setFailureTarget] = useState(null);
  const evaluations = usePackageEvaluations(projectId, revision);
  const notice = writebackNotice(evaluations.status);

  useEffect(() => {
    setPage(1); setData({ releases: [], total: 0 }); setResources(null);
    setReportTarget(null); setSuggestionsTarget(null); setFailureTarget(null);
  }, [projectId]);
  // 失败不阻断：点击“评测”时会重新读取并给出原因
  useEffect(() => { if (projectId) prefetchEvaluationEntry().catch(() => {}); }, [projectId]);
  useEffect(() => {
    if (!projectId) return undefined;
    let active = true;
    setLoading(true);
    setError('');
    api.packageReleases(projectId, { offset: (page - 1) * 10, limit: 10 })
      .then((result) => { if (active) setData(result); })
      .catch((e) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    api.packageResourcesStatus(projectId)
      .then((result) => { if (active) setResources(result); })
      .catch(() => { if (active) setResources({ pending: true }); });
    return () => { active = false; };
  }, [projectId, page, revision]);

  const download = async (releaseId) => {
    setDownloading(releaseId);
    try { await api.downloadRelease(projectId, releaseId); }
    catch (e) { message.error(e.message); }
    finally { setDownloading(''); }
  };
  // 版本的一种来源（评测 / 用户反馈）占一格：处理状态加上报告和优化建议两个链接
  const sourceCell = (row, source, feedback, empty) => {
    const target = source.entry && { runId: source.entry.run_id, version: row.version, releaseId: row.release_id, entry: source.entry, feedback };
    if (!target) return <Typography.Text type="secondary">{empty}</Typography.Text>;
    const state = processingState(source.entry);
    return (
      <Space size={10}>
        {state && <Tooltip title={state.hint}><Tag color={state.color} style={{ margin: 0 }}>{state.label}</Tag></Tooltip>}
        <ReasonButton {...LINK} reason={source.report.reason} onClick={() => setReportTarget(target)}>报告</ReasonButton>
        <ReasonButton {...LINK} reason={source.suggestions.reason} onClick={() => setSuggestionsTarget(target)}>优化建议</ReasonButton>
      </Space>
    );
  };
  const evaluationCell = (row) => {
    const source = releaseEvaluation(evaluations.status, row.release_id);
    const idle = <Tooltip title={source.report.reason}>{source.idle}</Tooltip>;
    return sourceCell(row, source, false, idle);
  };
  // 求解验证在发布后的独立评测中进行：只有发布时启用了求解契约、或已有验证记录的版本显示
  const solverValidation = (row) => {
    const validation = releaseSolverValidation(evaluations.status, row.release_id);
    if (!validation) return <Typography.Text type="secondary">—</Typography.Text>;
    const view = SOLVER_VALIDATION_STATES[validation.state];
    const target = validation.entry && { runId: validation.entry.run_id, version: row.version, releaseId: row.release_id };
    return (
      <Space size={10}>
        <Tooltip title={validation.reason}>
          <Tag color={view.color} style={{ margin: 0 }}>{view.label}</Tag>
        </Tooltip>
        {target && validation.state !== 'running' && (
          <Button {...LINK} onClick={() => setReportTarget(target)}>验证报告</Button>
        )}
        {['unverified', 'failed'].includes(validation.state) && (
          <Button {...LINK} onClick={() => openEvaluation(row, { runType: 'solver' }).catch((e) => message.error(e.message))}>
            去验证
          </Button>
        )}
      </Space>
    );
  };
  const hasSolver = data.releases.some((row) => releaseSolverValidation(evaluations.status, row.release_id));
  const optimized = (command) => {
    setSuggestionsTarget(null);
    setReportTarget(null);
    onClose();
    onCommand(command);
  };
  return (
    <Modal title={data.releases[0]?.name ? `已发布版本 — ${data.releases[0].name}` : '已发布版本'} open={!!projectId} onCancel={onClose} width={980}
      footer={<Button onClick={onClose}>关闭</Button>} destroyOnHidden>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">共 {data.total} 个版本。{EVALUATION_HINT}</Typography.Text>
        {resources?.pending && <Alert type="warning" showIcon message="共享资料状态暂不可用" />}
        {evaluations.status?.reachable === false && <Alert type="warning" showIcon message="评测服务暂不可用，评测状态可能不是最新" />}
        {notice && <Alert type={notice.type} showIcon message={notice.text}
          action={notice.type === 'warning' ? <Button size="small" onClick={evaluations.refresh}>重试</Button> : null} />}
        {error ? <Alert type="error" showIcon message={error}
          action={<Button size="small" onClick={() => setRevision((n) => n + 1)}>重试</Button>} /> : (
          <Table rowKey="release_id" loading={loading} dataSource={data.releases}
            locale={{ emptyText: <Empty description="暂无已发布版本" /> }}
            pagination={{ current: page, total: data.total, pageSize: 10, showSizeChanger: false, onChange: setPage }}
            columns={[
              { title: '版本', dataIndex: 'version', width: 270, render: (v, row, i) => (
                <>
                  <Space size={6}><strong>{v}</strong>{page === 1 && i === 0 && <Tag color="blue" style={{ margin: 0 }}>最新</Tag>}</Space>
                  <div style={{ whiteSpace: 'nowrap' }}><Typography.Text type="secondary" style={{ fontSize: 12 }}>{new Date(row.created_at).toLocaleString()} · {row.source_service}</Typography.Text></div>
                </>
              ) },
              { title: '评测', render: (_, row) => evaluationCell(row) },
              ...(hasSolver ? [{ title: '求解验证', render: (_, row) => solverValidation(row) }] : []),
              ...(hasFeedback(evaluations.status) ? [{ title: '用户反馈',
                render: (_, row) => sourceCell(row, releaseFeedback(evaluations.status, row.release_id), true, '—') }] : []),
              { title: '操作', width: 130, render: (_, row) => (
                <Space size={10}>
                  <Button {...LINK} onClick={() => openEvaluation(row).catch((e) => message.error(e.message))}>发起评测</Button>
                  <Tooltip title="下载这个版本的知识包，不会生成新版本">
                    <Button {...LINK} loading={downloading === row.release_id} onClick={() => download(row.release_id)}>下载</Button>
                  </Tooltip>
                </Space>
              ) },
            ]} />
        )}
        <Typography.Text type="secondary">Hub 仅保存明确共享的最新资料集，不与历史包版本绑定；本地资料不会自动上传。</Typography.Text>
      </Space>
      <EvaluationReportModal projectId={projectId} target={reportTarget} onClose={() => setReportTarget(null)} />
      <EvaluationSuggestionsModal projectId={projectId} target={suggestionsTarget}
        blockReason={suggestionsTarget ? optimizationBlock(evaluations.status, suggestionsTarget.releaseId, !!onCommand) : ''}
        onClose={() => setSuggestionsTarget(null)}
        onRegenerated={() => { setSuggestionsTarget(null); evaluations.refresh(); }}
        onOptimized={optimized}
        onReportFailure={suggestionsTarget && canReportFailure(evaluations.status, suggestionsTarget.releaseId, suggestionsTarget.entry)
          ? () => setFailureTarget(suggestionsTarget) : null} />
      <FailureDialog projectId={projectId} target={failureTarget} onClose={() => setFailureTarget(null)}
        onReported={() => { setFailureTarget(null); setSuggestionsTarget(null); evaluations.refresh(); }} />
    </Modal>
  );
}

// 按建议修改后无法发布新版本时，告诉评测服务这一轮处理失败及原因；成功的处理由发布新版本自动回写
function FailureDialog({ projectId, target, onClose, onReported }) {
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  useEffect(() => { setNote(''); }, [target]);
  const submit = async () => {
    setSaving(true);
    try {
      await api.reportPackageEvaluationFailure(projectId, target.runId, note.trim());
      message.success('已回写评测服务：这一轮优化建议处理失败');
      onReported();
    } catch (e) { message.error(e.message); }
    finally { setSaving(false); }
  };
  return (
    <Modal title={`标记处理失败 — 版本 ${target?.version || ''}`} open={!!target} onCancel={onClose} destroyOnHidden
      okText="提交" okButtonProps={{ disabled: !note.trim(), loading: saving }} onOk={submit}>
      <Typography.Paragraph type="secondary">建议无法落实、发布不了新版本时使用。评测服务会记录“处理失败”和原因；之后修改并发布新版本，仍会自动变为“已处理”。</Typography.Paragraph>
      <Input.TextArea rows={4} maxLength={1000} showCount value={note} onChange={(e) => setNote(e.target.value)}
        placeholder="例如：建议要求的数据表在业务库中不存在" />
    </Modal>
  );
}
