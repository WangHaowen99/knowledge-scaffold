// 发布页「已发布版本」：一张表合并原来的发布记录（PB-04，本地：降级、包清单、图模式变体）与已发布版本（HB-05，
// develop PublishedVersionsDialog，ADR-0098：Hub 上这个项目所在包的全部版本，含别人发布到同一个包的，每页 10 条）。
// 评测、求解验证、用户反馈三格与「发起评测」用评测对接的组件；「对比」挂版本对比抽屉；下载标准包走 HB-06，
// 图模式变体与包清单走本地留存（PB-06、PB-07）。Hub 读不到时退回本地记录。共享用户数与「知识包版本对比」在表头。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Icon, Menu, Note } from '../../ui/index.jsx';
import {
  EvaluationCell, EvaluationNotices, FeedbackCell, SolverValidationCell, StartEvaluation,
} from '../evaluation/ReleaseEvaluation.jsx';
import { usePopMenu } from '../versions/popMenu.js';
import { fmtStamp } from '../versions/versionsModel.js';
import { VERSIONS_PAGE, localReleaseRows, releaseRows, releaseSource } from './publishCheckModel.js';
import { releaseGate } from './publishModel.js';

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

// 下载与包清单：标准包走 Hub；图模式变体与包清单只有本地留存的才有
function DownloadMenu({ row, busy, onHub, onLocal, onManifest }) {
  const menu = usePopMenu({ width: 170, height: 130 });
  const items = [
    { label: '下载标准包', icon: 'download', disabled: busy, onClick: () => (row.release_id ? onHub(row) : onLocal(row.local, 'standard')) },
    ...(row.local ? [
      { label: '下载图模式包', icon: 'graph', onClick: () => onLocal(row.local, 'graph') },
      { label: '查看包清单', icon: 'list', onClick: () => onManifest(row.local) },
    ] : []),
  ];
  return (
    <span ref={menu.ref}>
      <span className="pb-link xs row g4" role="button" onClick={menu.toggle}>{busy ? '下载中…' : '下载'}<Icon name="chevd" size={12} /></span>
      {menu.at ? <Menu className="pb-menu" style={menu.at} items={items} onPick={menu.close} /> : null}
    </span>
  );
}

export default function PublishedVersions({
  projectId, projectName = '', ev, records, refreshKey, sharing, actions, onCompare, onManifest, onDownloadLocal,
}) {
  const { api, services } = useApp();
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ releases: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [downloading, setDownloading] = useState('');

  useEffect(() => { setPage(1); setData({ releases: [], total: 0 }); }, [projectId]);
  useEffect(() => {
    if (!projectId) return undefined;
    let active = true;
    setLoading(true);
    setError('');
    api.projectHubReleases(projectId, (page - 1) * VERSIONS_PAGE, VERSIONS_PAGE)
      .then((result) => { if (active) setData({ releases: result.releases || result.items || [], total: result.total || 0 }); })
      .catch((e) => { if (active) setError(errorText(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api, projectId, page, revision, refreshKey]);

  const downloadHub = async (row) => {
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
  // Hub 读不到时退回本地记录（还能下载、看清单，评测要等 Hub 恢复）
  const offline = !!error;
  const rows = offline ? localReleaseRows(records) : releaseRows(data.releases, records);
  const total = offline ? rows.length : data.total;
  const solver = !offline && ev?.hasSolver(rows);
  const feedback = !offline && !!ev?.hasFeedback;
  const cols = 3 + (solver ? 1 : 0) + (feedback ? 1 : 0);

  return (
    <section id="pb-releases" className="card pb-releases">
      <div className="card-h">
        <span style={{ color: 'var(--blue)' }}><Icon name="history" /></span>
        <h3>已发布版本</h3>
        {total ? <span className="xs muted">{total} 个</span> : null}
        <span className="sp" />
        {actions}
        {sharing}
        <Button size="sm" icon="split" disabled={!total} onClick={() => onCompare({})}>版本对比</Button>
      </div>
      <div className="card-b">
        {offline ? (
          <Note tone="amber" className="mb8">
            <span className="row"><span className="grow">知识包 Hub 暂时读不到，先列本机的发布记录：{error}</span>
              <Button size="xs" kind="ghost" onClick={() => setRevision((n) => n + 1)}>重试</Button></span>
          </Note>
        ) : null}
        {ev && !offline ? <EvaluationNotices ev={ev} /> : null}
        {!rows.length ? (
          <div className="sm muted">{loading ? '正在读取…' : '还没有发布过'}</div>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl compact">
              <thead>
                <tr>
                  <th style={{ width: 230 }}>版本</th>
                  {offline ? null : <th>评测</th>}
                  {solver ? <th>求解验证</th> : null}
                  {feedback ? <th>用户反馈</th> : null}
                  <th style={{ width: 210 }} />
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => {
                  const gate = row.local ? releaseGate(row.local) : null;
                  const meta = [row.created_at ? fmtStamp(row.created_at) : '', releaseSource(row), row.local?.version_label ? `来自 ${row.local.version_label}` : '']
                    .filter(Boolean).join(' · ');
                  return (
                    <tr key={row.release_id || row.version}>
                      <td>
                        <span className="row g6">
                          <b>{row.version}</b>
                          {page === 1 && i === 0 ? <Chip sm tone="blue">最新</Chip> : null}
                          {gate?.chip[0] === '降级发布' ? <Chip sm tone="amber" title={gate.note || undefined}>降级发布</Chip> : null}
                        </span>
                        <div className="xs muted" style={{ whiteSpace: 'nowrap' }}>{meta}</div>
                      </td>
                      {offline ? null : <td>{ev ? <EvaluationCell ev={ev} release={row} /> : null}</td>}
                      {solver ? <td><SolverValidationCell ev={ev} release={row} /></td> : null}
                      {feedback ? <td><FeedbackCell ev={ev} release={row} /></td> : null}
                      <td>
                        <span className="row g12" style={{ justifyContent: 'flex-end' }}>
                          {ev && !offline ? <StartEvaluation ev={ev} release={row} /> : null}
                          {!offline ? <span className="pb-link xs" role="button" onClick={() => onCompare({ releaseId: row.release_id, version: row.version })}>对比</span> : null}
                          <DownloadMenu row={row} busy={downloading === row.release_id} onHub={downloadHub} onLocal={onDownloadLocal} onManifest={onManifest} />
                        </span>
                      </td>
                    </tr>
                  );
                })}
                {loading && !rows.length ? <tr><td colSpan={cols} className="sm muted">正在读取…</td></tr> : null}
              </tbody>
            </table>
          </div>
        )}
        {!offline ? <Pager page={page} total={total} onPage={setPage} /> : null}
      </div>
    </section>
  );
}
