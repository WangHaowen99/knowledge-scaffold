// 从知识包 Hub 导入（ADR-0091，HB-01 / HB-02 / HB-04）：选本人可见的包、选一个版本、起名，导入成新项目。
// 导入的项目之后发布会成为新的 Hub 包（包身份按项目定）。设计稿第五版没有这一入口，放在导入页（见 develop-sync P1 报告）。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Field, Modal, Note, Table, cx } from '../../ui/index.jsx';

const PAGE = 20;
const short = (digest) => String(digest || '').slice(0, 12);

export default function HubImportModal({ open, onClose }) {
  const { api } = useApp();
  const [packages, setPackages] = useState(null);
  const [pkg, setPkg] = useState(null);
  const [releases, setReleases] = useState(null);
  const [release, setRelease] = useState(null);
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    let live = true;
    setPackages(null); setPkg(null); setError('');
    api.hubPackages(0, PAGE).then((r) => live && setPackages(r.items || [])).catch((e) => live && setError(errorText(e)));
    return () => { live = false; };
  }, [open, api]);

  useEffect(() => {
    setRelease(null); setReleases(null);
    if (!pkg) return undefined;
    let live = true;
    setName(pkg.name || '');
    api.hubPackageReleases(pkg.ontology_id, 0, PAGE).then((r) => live && setReleases(r.items || [])).catch((e) => live && setError(errorText(e)));
    return () => { live = false; };
  }, [pkg, api]);

  const submit = async () => {
    setBusy(true);
    try {
      const meta = await api.importHubPackage({
        name: name.trim(), ontology_id: pkg.ontology_id, release_id: release.release_id, package_digest: release.package_digest,
      });
      toast(`已导入「${meta.name}」`, 'check');
      onClose();
      navigate(pagePath('wb-l2', meta.id));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const pick = (list, selected, key, onPick, cells) => list.map((item) => {
    const on = selected?.[key] === item[key];
    return cells(item).map((cell, i) => (
      <span key={i} className={cx('ell', on && 'b')} role="button" style={{ cursor: 'pointer' }} onClick={() => onPick(item)}>{cell}</span>
    ));
  });

  return (
    <Modal
      open={open}
      onClose={busy ? undefined : onClose}
      icon="pkg"
      title="从 Hub 导入"
      w={760}
      foot={(
        <>
          <span className="sp" />
          <Button disabled={busy} onClick={onClose}>取消</Button>
          <Button kind="primary" disabled={!release || !name.trim() || busy} onClick={submit}>{busy ? '正在导入…' : '导入'}</Button>
        </>
      )}
    >
      {error ? <Note tone="red" className="mb12">{error}</Note> : null}
      {packages && !packages.length ? <div className="sm muted">没有可导入的知识包</div> : null}
      {packages?.length ? (
        <Table compact cols={['知识包', { t: '最新版本', w: 110 }]} rows={pick(packages, pkg, 'ontology_id', setPkg, (p) => [p.name, p.version])} />
      ) : null}
      {releases?.length ? (
        <div className="mt12">
          <Table compact cols={[{ t: '版本', w: 110 }, '提交时间', { t: '摘要', w: 130 }]} rows={pick(releases, release, 'release_id', setRelease, (r) => [r.version, String(r.committed_at || '').slice(0, 16).replace('T', ' '), short(r.package_digest)])} />
        </div>
      ) : null}
      {pkg ? (
        <Field label="项目名称" req className="mt12">
          <input className="inp" value={name} maxLength={128} disabled={busy} onChange={(e) => setName(e.target.value)} />
        </Field>
      ) : null}
    </Modal>
  );
}
