// 导入已发布知识包（develop HubPackagesDialog 的元素与文字，ADR-0098；HB-01 / HB-02 / HB-04）：
// 本人在知识包 Hub 上的包（每页 20）→ 选一个固定版本（每页 20）→ 起名，导入成新建模项目并打开。
// 入口：项目列表「导入已有知识包 ▾ → 已发布知识包」、导入页顶部「已发布知识包」。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Inp, Modal, Note, Radio } from '../../ui/index.jsx';

const PAGE = 20;
const EMPTY = { items: [], total: 0 };

function Pager({ page, total, onPage }) {
  const pages = Math.max(1, Math.ceil(total / PAGE));
  if (pages <= 1) return null;
  return (
    <div className="row g6 mt8" style={{ justifyContent: 'flex-end' }}>
      <Button size="xs" kind="ghost" icon="chevl" disabled={page <= 1} onClick={() => onPage(page - 1)} />
      <span className="xs muted">{page} / {pages}</span>
      <Button size="xs" kind="ghost" icon="chev" disabled={page >= pages} onClick={() => onPage(page + 1)} />
    </div>
  );
}

// 单选表格：点行选中
function PickTable({ cols, rows, rowKey, selected, onPick, loading }) {
  return (
    <div className="tbl-wrap">
      <table className="tbl compact">
        <thead><tr><th style={{ width: 32 }} />{cols.map((c) => <th key={c.title} style={c.w ? { width: c.w } : undefined}>{c.title}</th>)}</tr></thead>
        <tbody>
          {loading && !rows.length ? <tr><td colSpan={cols.length + 1} className="sm muted">正在读取…</td></tr> : null}
          {!loading && !rows.length ? <tr><td colSpan={cols.length + 1} className="sm muted" style={{ textAlign: 'center', padding: 18 }}>暂无数据</td></tr> : null}
          {rows.map((row) => {
            const on = selected?.[rowKey] === row[rowKey];
            return (
              <tr key={row[rowKey]} onClick={() => onPick(row)} style={{ cursor: 'pointer' }}>
                <td><Radio on={on} onChange={() => onPick(row)} /></td>
                {cols.map((c) => <td key={c.title} className="ell" style={{ maxWidth: 0 }} title={String(row[c.key] ?? '')}>{row[c.key]}</td>)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function HubImportModal({ open, onClose, onImported }) {
  const { api } = useApp();
  const [packages, setPackages] = useState(EMPTY);
  const [versions, setVersions] = useState(EMPTY);
  const [selected, setSelected] = useState(null);
  const [release, setRelease] = useState(null);
  const [packagePage, setPackagePage] = useState(1);
  const [versionPage, setVersionPage] = useState(1);
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    setLoading(true);
    setError('');
    api.hubPackages((packagePage - 1) * PAGE, PAGE)
      .then((data) => { if (active) setPackages(data); })
      .catch((e) => { if (active) setError(errorText(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api, open, packagePage, revision]);

  useEffect(() => {
    setRelease(null);
    setVersions(EMPTY);
    if (!open || !selected) return undefined;
    let active = true;
    api.hubPackageReleases(selected.ontology_id, (versionPage - 1) * PAGE, PAGE)
      .then((data) => { if (active) setVersions(data); })
      .catch((e) => { if (active) setError(errorText(e)); });
    return () => { active = false; };
  }, [api, open, selected, versionPage, revision]);

  const importPackage = async () => {
    setBusy(true);
    try {
      const result = await api.importHubPackage({
        name: name.trim(), ontology_id: selected.ontology_id, release_id: release.release_id, package_digest: release.package_digest,
      });
      toast('已导入，可继续建模', 'check');
      onClose();
      if (onImported) await onImported(result);
      else navigate(pagePath(result.landing_page || 'wb-l2', result.id));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={busy ? undefined : onClose}
      icon="pkg"
      title="导入已发布知识包"
      w={900}
      foot={(
        <>
          <span className="sp" />
          <Button disabled={busy} onClick={onClose}>关闭</Button>
          <Button kind="primary" disabled={!release || !name.trim() || busy} onClick={importPackage}>{busy ? '正在导入…' : '导入并继续建模'}</Button>
        </>
      )}
    >
      <div className="sm muted mb12">只显示当前账号拥有的独立知识包。选择固定版本创建建模项目，后续发布保留包身份及基础版本。</div>
      {error ? (
        <Note tone="red" className="mb12">
          <span className="row"><span className="grow">{error}</span><Button size="xs" kind="ghost" onClick={() => setRevision((v) => v + 1)}>重试</Button></span>
        </Note>
      ) : null}
      <PickTable
        cols={[{ title: '知识包名称', key: 'name', w: '33%' }, { title: '版本', key: 'version', w: '17%' }, { title: '知识包 ID', key: 'ontology_id', w: '50%' }]}
        rows={packages.items || []} rowKey="ontology_id" selected={selected} loading={loading}
        onPick={(row) => { setSelected(row); setVersionPage(1); setName(''); }}
      />
      <Pager page={packagePage} total={packages.total || 0} onPage={setPackagePage} />
      {selected ? (
        <div className="mt12">
          <PickTable
            cols={[{ title: '版本', key: 'version' }, { title: '来源', key: 'source_service' }, { title: '摘要', key: 'package_digest' }]}
            rows={versions.items || []} rowKey="release_id" selected={release} onPick={setRelease}
          />
          <Pager page={versionPage} total={versions.total || 0} onPage={setVersionPage} />
        </div>
      ) : null}
      <Inp className="mt12" aria-label="新建模项目名称" placeholder="新建模项目名称" value={name} maxLength={128} disabled={busy}
        onChange={(e) => setName(e.target.value)} />
    </Modal>
  );
}
