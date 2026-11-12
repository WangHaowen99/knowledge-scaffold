// 已发布版本（develop PublishedVersionsDialog 的元素与文字，ADR-0098）：当前用户在知识包 Hub 上能看到的、这个项目所在包的
// 全部版本（HB-05，含别人发布到同一个包的），每页 10 条。评测、求解验证、用户反馈三格与「发起评测」用评测对接的组件，
// 「知识包版本对比」「与其他版本对比」挂版本对比的抽屉，「下载」走 HB-06。发布成功后自动打开。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Modal, Note } from '../../ui/index.jsx';
import {
  EvaluationCell, EvaluationNotices, FeedbackCell, SolverValidationCell,
} from '../evaluation/ReleaseEvaluation.jsx';
import { EVALUATION_HINT, RESOURCES_NOTE, VERSIONS_PAGE, versionMeta, versionsTitle } from './publishCheckModel.js';

function Pager({ page, total, onPage }) {
  const pages = Math.max(1, Math.ceil(total / VERSIONS_PAGE));
  if (pages <= 1) return null;
  return (
    <div className="row g6 mt8" style={{ justifyContent: 'flex-end' }}>
      <Button size="xs" kind="ghost" icon="chevl" disabled={page <= 1} onClick={() => onPage(page - 1)} />
      <span className="xs muted">{page} / {pages}</span>
      <Button size="xs" kind="ghost" icon="chev" disabled={page >= pages} onClick={() => onPage(page + 1)} />
    </div>
  );
}

export default function PublishedVersions({ open, projectId, projectName = '', ev, onClose, onCompare }) {
  const { api, services } = useApp();
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ releases: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [downloading, setDownloading] = useState('');
  const [resources, setResources] = useState(null);

  useEffect(() => { setPage(1); setData({ releases: [], total: 0 }); setResources(null); }, [projectId]);
  useEffect(() => {
    if (!open || !projectId) return undefined;
    let active = true;
    setLoading(true);
    setError('');
    api.projectHubReleases(projectId, (page - 1) * VERSIONS_PAGE, VERSIONS_PAGE)
      .then((result) => { if (active) setData({ releases: result.releases || result.items || [], total: result.total || 0 }); })
      .catch((e) => { if (active) setError(errorText(e)); })
      .finally(() => { if (active) setLoading(false); });
    api.projectHubResourcesStatus(projectId)
      .then((result) => { if (active) setResources(result); })
      .catch(() => { if (active) setResources({ pending: true }); });
    return () => { active = false; };
  }, [api, open, projectId, page, revision]);

  const download = async (row) => {
    setDownloading(row.release_id);
    try {
      await services.download(
        `/projects/${encodeURIComponent(projectId)}/hub/releases/${encodeURIComponent(row.release_id)}/download`,
        `${row.name || projectName || 'package'}-${row.version}.zip`,
      );
    } catch (e) {
      toast(errorText(e), 'alert');
    } finally {
      setDownloading('');
    }
  };
  const rows = data.releases;
  const solver = ev?.hasSolver(rows);
  const feedback = !!ev?.hasFeedback;

  return (
    <Modal open={open} onClose={onClose} icon="history" title={versionsTitle(data, projectName)} w={980}
      foot={<><span className="sp" /><Button onClick={onClose}>关闭</Button></>}>
      <div className="row g12 mb12">
        <span className="grow sm muted">共 {data.total} 个版本。{EVALUATION_HINT}</span>
        <Button kind="primary" size="sm" onClick={() => onCompare(null)}>知识包版本对比</Button>
      </div>
      {resources?.pending ? <Note tone="amber" className="mb8">共享资料状态暂不可用</Note> : null}
      {ev ? <EvaluationNotices ev={ev} /> : null}
      {error ? (
        <Note tone="red" className="mb8">
          <span className="row"><span className="grow">{error}</span><Button size="xs" kind="ghost" onClick={() => setRevision((n) => n + 1)}>重试</Button></span>
        </Note>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl compact">
            <thead>
              <tr>
                <th style={{ width: 270 }}>版本</th>
                <th>评测</th>
                {solver ? <th>求解验证</th> : null}
                {feedback ? <th>用户反馈</th> : null}
                <th style={{ width: 220 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {loading && !rows.length ? (
                <tr><td colSpan={5} className="sm muted">正在读取…</td></tr>
              ) : null}
              {!loading && !rows.length ? (
                <tr><td colSpan={5} className="sm muted" style={{ textAlign: 'center', padding: 24 }}>暂无已发布版本</td></tr>
              ) : null}
              {rows.map((row, i) => (
                <tr key={row.release_id}>
                  <td>
                    <span className="row g6"><b>{row.version}</b>{page === 1 && i === 0 ? <Chip sm tone="blue">最新</Chip> : null}</span>
                    <div className="xs muted" style={{ whiteSpace: 'nowrap' }}>{versionMeta(row)}</div>
                  </td>
                  <td>{ev ? <EvaluationCell ev={ev} release={row} /> : null}</td>
                  {solver ? <td><SolverValidationCell ev={ev} release={row} /></td> : null}
                  {feedback ? <td><FeedbackCell ev={ev} release={row} /></td> : null}
                  <td>
                    <span className="row g12">
                      <span className="xs pb-link" role="button" onClick={() => onCompare({ releaseId: row.release_id, version: row.version })}>与其他版本对比</span>
                      {ev ? <span className="xs pb-link" role="button" onClick={() => ev.evaluate(row)}>发起评测</span> : null}
                      <span className="xs pb-link" role="button" title="下载这个版本的知识包，不会生成新版本"
                        onClick={() => downloading !== row.release_id && download(row)}>{downloading === row.release_id ? '下载中…' : '下载'}</span>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pager page={page} total={data.total} onPage={setPage} />
      <div className="xs muted mt12">{RESOURCES_NOTE}</div>
    </Modal>
  );
}
