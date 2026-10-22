import { useEffect, useState } from 'react';
import { Alert, Button, Empty, Modal, Space, Table, Tag, Typography, message } from 'antd';
import { api } from '../lib/api';

export default function PublishedVersionsDialog({ projectId, onClose }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ releases: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [downloading, setDownloading] = useState('');
  const [resources, setResources] = useState(null);

  useEffect(() => { setPage(1); setData({ releases: [], total: 0 }); setResources(null); }, [projectId]);
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
  return (
    <Modal title="已发布版本" open={!!projectId} onCancel={onClose} width={900}
      footer={<Button onClick={onClose}>关闭</Button>} destroyOnHidden>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">当前账号拥有 {data.total} 个已发布版本。下载所选版本不会生成新版本。</Typography.Text>
        {resources?.pending && <Alert type="warning" showIcon message="项目最新资料待同步" />}
        {error ? <Alert type="error" showIcon message={error}
          action={<Button size="small" onClick={() => setRevision((n) => n + 1)}>重试</Button>} /> : (
          <Table rowKey="release_id" loading={loading} dataSource={data.releases}
            locale={{ emptyText: <Empty description="暂无已发布版本" /> }}
            pagination={{ current: page, total: data.total, pageSize: 10, showSizeChanger: false, onChange: setPage }}
            columns={[
              { title: '版本', dataIndex: 'version', render: (v, row, i) => <Space><strong>{v}</strong>{page === 1 && i === 0 && <Tag color="blue">最新</Tag>}</Space> },
              { title: '发布时间', dataIndex: 'created_at', render: (v) => new Date(v).toLocaleString() },
              { title: '来源', dataIndex: 'source_service' },
              { title: '操作', render: (_, row) => <Button type="link" loading={downloading === row.release_id}
                onClick={() => download(row.release_id)}>下载知识包</Button> },
            ]} />
        )}
        <Typography.Text type="secondary">原始资料仅保存项目最新状态，不提供资料历史版本。</Typography.Text>
      </Space>
    </Modal>
  );
}
