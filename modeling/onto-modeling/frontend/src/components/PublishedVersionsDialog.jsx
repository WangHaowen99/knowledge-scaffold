import { useEffect, useState } from 'react';
import { Alert, Button, Empty, Modal, Space, Table, Tag, Typography, message } from 'antd';
import { api } from '../lib/api';
import { openEvaluation, prefetchEvaluationEntry } from '../lib/evaluation.js';
import { EVALUATION_HINT, optimizationBlock, releaseEvaluation } from '../lib/packageEvaluation.js';
import { EvaluationReportModal, EvaluationSuggestionsModal, ReasonButton, usePackageEvaluations } from './PackageEvaluationModals.jsx';

// 置灰原因放在按钮正右方，不遮挡上一行的操作按钮；链接按钮自带约 15px 内边距，左移 5px 让提示离文字近一些、仍留间隔
const REASON_TOOLTIP = { placement: 'right', align: { offset: [-5, 0] } };

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
  const evaluations = usePackageEvaluations(projectId, revision);

  useEffect(() => {
    setPage(1); setData({ releases: [], total: 0 }); setResources(null);
    setReportTarget(null); setSuggestionsTarget(null);
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
  const evaluationActions = (row) => {
    const { entry, report, suggestions } = releaseEvaluation(evaluations.status, row.release_id);
    const target = entry && { runId: entry.run_id, version: row.version, releaseId: row.release_id };
    return (
      <>
        <ReasonButton type="link" reason={report.reason} tooltip={REASON_TOOLTIP} onClick={() => setReportTarget(target)}>评测报告</ReasonButton>
        <ReasonButton type="link" reason={suggestions.reason} tooltip={REASON_TOOLTIP} onClick={() => setSuggestionsTarget(target)}>优化建议</ReasonButton>
      </>
    );
  };
  const optimized = (command) => {
    setSuggestionsTarget(null);
    setReportTarget(null);
    onClose();
    onCommand(command);
  };
  return (
    <Modal title="已发布版本" open={!!projectId} onCancel={onClose} width={900}
      footer={<Button onClick={onClose}>关闭</Button>} destroyOnHidden>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">当前账号拥有 {data.total} 个已发布版本。下载所选版本不会生成新版本。{EVALUATION_HINT}</Typography.Text>
        {resources?.pending && <Alert type="warning" showIcon message="共享资料状态暂不可用" />}
        {evaluations.status?.reachable === false && <Alert type="warning" showIcon message="评测服务暂不可用，评测状态可能不是最新" />}
        {error ? <Alert type="error" showIcon message={error}
          action={<Button size="small" onClick={() => setRevision((n) => n + 1)}>重试</Button>} /> : (
          <Table rowKey="release_id" loading={loading} dataSource={data.releases}
            locale={{ emptyText: <Empty description="暂无已发布版本" /> }}
            pagination={{ current: page, total: data.total, pageSize: 10, showSizeChanger: false, onChange: setPage }}
            columns={[
              { title: '包名称', dataIndex: 'name', ellipsis: true },
              { title: '版本', dataIndex: 'version', render: (v, row, i) => <Space><strong>{v}</strong>{page === 1 && i === 0 && <Tag color="blue">最新</Tag>}</Space> },
              { title: '发布时间', dataIndex: 'created_at', render: (v) => new Date(v).toLocaleString() },
              { title: '来源', dataIndex: 'source_service' },
              { title: '操作', render: (_, row) => <Space size={0} wrap><Button type="link" loading={downloading === row.release_id}
                onClick={() => download(row.release_id)}>下载知识包</Button><Button type="link" onClick={() => openEvaluation(row).catch((e) => message.error(e.message))}>评测</Button>{evaluationActions(row)}</Space> },
            ]} />
        )}
        <Typography.Text type="secondary">Hub 仅保存明确共享的最新资料集，不与历史包版本绑定；本地资料不会自动上传。</Typography.Text>
      </Space>
      <EvaluationReportModal projectId={projectId} target={reportTarget} onClose={() => setReportTarget(null)} />
      <EvaluationSuggestionsModal projectId={projectId} target={suggestionsTarget}
        blockReason={suggestionsTarget ? optimizationBlock(evaluations.status, suggestionsTarget.releaseId, !!onCommand) : ''}
        onClose={() => setSuggestionsTarget(null)}
        onRegenerated={() => { setSuggestionsTarget(null); evaluations.refresh(); }}
        onOptimized={optimized} />
    </Modal>
  );
}
