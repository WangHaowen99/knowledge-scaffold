import { useEffect, useState } from 'react';
import { Alert, Button, Input, Modal, Space, Table, Typography, message } from 'antd';
import { api } from '../lib/api.js';
import { openEvaluation, prefetchEvaluationEntry } from '../lib/evaluation.js';

export default function HubPackagesDialog({ open, onClose, onImported }) {
  const [packages, setPackages] = useState({ items: [], total: 0 });
  const [versions, setVersions] = useState({ items: [], total: 0 });
  const [selected, setSelected] = useState(null);
  const [release, setRelease] = useState(null);
  const [packagePage, setPackagePage] = useState(1);
  const [versionPage, setVersionPage] = useState(1);
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);

  // 失败不阻断：点击“评测”时会重新读取并给出原因
  useEffect(() => { if (open) prefetchEvaluationEntry().catch(() => {}); }, [open]);
  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    setLoading(true);
    setError('');
    api.hubPackages((packagePage - 1) * 20)
      .then((data) => { if (active) setPackages(data); })
      .catch((e) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [open, packagePage, revision]);

  useEffect(() => {
    setRelease(null);
    setVersions({ items: [], total: 0 });
    if (!open || !selected) return undefined;
    let active = true;
    api.hubPackageVersions(selected.hub_package_id, (versionPage - 1) * 20)
      .then((data) => { if (active) setVersions(data); })
      .catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [open, selected, versionPage, revision]);

  const importPackage = async () => {
    setBusy(true);
    try {
      const result = await api.importHubPackage({ name: name.trim(), hub_package_id: selected.hub_package_id,
        release_id: release.release_id, package_digest: release.package_digest });
      message.success('已导入，可继续建模');
      onClose();
      await onImported(result);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  return <Modal title="我的 Hub 知识包" open={open} onCancel={busy ? undefined : onClose} width={900}
    footer={<Space><Button disabled={busy} onClick={onClose}>关闭</Button>
      <Button type="primary" loading={busy} disabled={!release || !name.trim()} onClick={importPackage}>导入并继续建模</Button></Space>}>
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text type="secondary">只显示当前账号拥有的独立知识包。选择固定版本创建建模项目，后续发布保留包身份及基础版本。</Typography.Text>
      {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => setRevision((v) => v + 1)}>重试</Button>} />}
      <Table size="small" rowKey="hub_package_id" loading={loading} dataSource={packages.items}
        rowSelection={{ type: 'radio', selectedRowKeys: selected ? [selected.hub_package_id] : [],
          onChange: (_, rows) => { setSelected(rows[0]); setVersionPage(1); setName(''); } }}
        pagination={{ current: packagePage, pageSize: 20, total: packages.total, showSizeChanger: false, onChange: setPackagePage }}
        columns={[{ title: '包 ID', dataIndex: 'hub_package_id', ellipsis: true }, { title: '本体标识', dataIndex: 'ontology_id', ellipsis: true }]} />
      {selected && <Table size="small" rowKey="release_id" dataSource={versions.items}
        rowSelection={{ type: 'radio', selectedRowKeys: release ? [release.release_id] : [], onChange: (_, rows) => setRelease(rows[0]) }}
        pagination={{ current: versionPage, pageSize: 20, total: versions.total, showSizeChanger: false, onChange: setVersionPage }}
        columns={[{ title: '版本', dataIndex: 'version' }, { title: '来源', dataIndex: 'source_service' },
          { title: '摘要', dataIndex: 'package_digest', ellipsis: true },
          { title: '操作', render: (_, row) => <Button onClick={() => openEvaluation({ ...row, hub_package_id: selected.hub_package_id }).catch((e) => message.error(e.message))}>评测</Button> }]} />}
      <Input aria-label="新建模项目名称" placeholder="新建模项目名称" value={name} maxLength={128} disabled={busy} onChange={(e) => setName(e.target.value)} />
    </Space>
  </Modal>;
}
