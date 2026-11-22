import { FileText, RefreshCw, Square } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import DocDrawer from './DocDrawer';
import { offers, type BackendInfo } from './backend';
import { formatTime, type Tone } from './format';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { DocumentItem, KnowledgeBase } from './types';
import { Badge, Button, Empty, Modal, Spinner } from './ui';
import { ContentView } from './views';

export interface Job {
  id: string;
  kb_id: string | null;
  doc_id: string | null;
  action: string;
  status: string;
  progress: number | null;
  message: string | null;
  cancellable: boolean;
  has_report: boolean;
  started_at: string | null;
  finished_at: string | null;
}

const STATUS: Record<string, { label: string; tone: Tone }> = {
  queued: { label: '排队中', tone: 'neutral' },
  running: { label: '进行中', tone: 'info' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'warning' },
};

export const jobActive = (job: Job) => job.status === 'queued' || job.status === 'running';

export interface JobsState {
  items: Job[] | null;
  running: number;
  refresh: () => void;
}

/**
 * Jobs of the knowledge base on screen, when its backend has them. Polled only while one is queued or
 * running; ``onSettled`` runs when one of those finishes (its results may change counts and documents).
 */
export function useJobs(kb: KnowledgeBase | null, onSettled: () => void): JobsState {
  const kbId = kb?.id ?? null;
  const enabled = offers(kb, 'jobs');
  const [items, setItems] = useState<Job[] | null>(null);
  const settled = useRef(onSettled);
  settled.current = onSettled;
  const active = useRef<Set<string> | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled || !kbId) return;
    try {
      const res = await rpc.call<{ items: Job[] }>('jobs.list', { kb_id: kbId });
      const now = new Set(res.items.filter(jobActive).map((j) => j.id));
      if (active.current && [...active.current].some((id) => !now.has(id))) settled.current();
      active.current = now;
      setItems(res.items);
    } catch {
      setItems((cur) => cur ?? []);
    }
  }, [enabled, kbId]);

  useEffect(() => {
    setItems(null);
    active.current = null;
    void refresh();
  }, [refresh]);

  const running = items?.filter(jobActive).length ?? 0;
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [running, refresh]);

  const refreshNow = useCallback(() => void refresh(), [refresh]);
  return { items, running, refresh: refreshNow };
}

function ReportModal({ job, title, onClose }: { job: Job; title: string; onClose: () => void }) {
  const [report, setReport] = useState<{ content: unknown } | { error: string } | null>(null);
  useEffect(() => {
    rpc
      .call<Job & { report?: { media_type: string; content: unknown } }>('jobs.get', { job_id: job.id, report: true })
      .then((res) => setReport({ content: res.report?.content ?? null }))
      .catch((err) => setReport({ error: errorText(err) }));
  }, [job.id]);
  return (
    <Modal title={title} width={760} onClose={onClose}>
      <div className="report-body">
        {report === null ? (
          <div className="empty">
            <Spinner size={18} />
          </div>
        ) : 'error' in report ? (
          <div className="error-box">{report.error}</div>
        ) : (
          <ContentView content={report.content} />
        )}
      </div>
    </Modal>
  );
}

export function JobsTab({ kb, backend, jobs }: { kb: KnowledgeBase; backend: BackendInfo | undefined; jobs: JobsState }) {
  const notify = useToast();
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [report, setReport] = useState<Job | null>(null);
  const [openDoc, setOpenDoc] = useState<string | null>(null);
  const { items, refresh } = jobs;
  const labelOf = (job: Job) => backend?.actions?.find((a) => a.id === job.action)?.label ?? job.action;

  // opening the tab shows the current state even when nothing was running (no polling then)
  useEffect(() => refresh(), []);

  // document jobs show the document's title, looked up once per document
  useEffect(() => {
    const ids = (items ?? []).flatMap((j) => (j.doc_id && !(j.doc_id in titles) ? [j.doc_id] : []));
    const missing = Array.from(new Set(ids));
    if (!missing.length) return;
    setTitles((cur) => ({ ...cur, ...Object.fromEntries(missing.map((id) => [id, ''])) }));
    for (const id of missing) {
      rpc
        .call<DocumentItem>('docs.get', { doc_id: id })
        .then((doc) => setTitles((cur) => ({ ...cur, [id]: doc.title })))
        .catch(() => undefined);
    }
  }, [items, titles]);

  const cancel = async (job: Job) => {
    try {
      await rpc.call<Job>('jobs.cancel', { job_id: job.id });
      notify('已取消');
    } catch (err) {
      notify(errorText(err), true);
    }
    refresh();
  };

  if (items === null) {
    return (
      <div className="empty">
        <Spinner size={18} />
      </div>
    );
  }
  return (
    <div>
      <div className="toolbar">
        <span className="grow" />
        <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={refresh} title="刷新" />
      </div>
      {items.length === 0 ? (
        <Empty title="还没有任务" />
      ) : (
        <table className="table table-static">
          <thead>
            <tr>
              <th>任务</th>
              <th>状态</th>
              <th>开始</th>
              <th>结束</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((job) => {
              const st = STATUS[job.status] ?? { label: job.status, tone: 'neutral' as Tone };
              const active = jobActive(job);
              return (
                <tr key={job.id}>
                  <td>
                    <div className="doc-title">
                      {labelOf(job)}
                      {job.doc_id ? (
                        <button className="link-btn job-doc" onClick={() => setOpenDoc(job.doc_id)}>
                          {titles[job.doc_id] || job.doc_id}
                        </button>
                      ) : null}
                    </div>
                    {job.message ? (
                      <div className={`doc-sub ${job.status === 'failed' ? 'field-error' : ''}`} title={job.message}>
                        {job.message}
                      </div>
                    ) : null}
                  </td>
                  <td>
                    <div className="job-status">
                      <Badge tone={st.tone}>
                        {active ? <Spinner size={10} /> : null}
                        {st.label}
                      </Badge>
                      {active && job.progress !== null ? (
                        <>
                          <div className="progress job-progress">
                            <div style={{ width: `${Math.round(job.progress * 100)}%` }} />
                          </div>
                          <span className="num muted">{Math.round(job.progress * 100)}%</span>
                        </>
                      ) : null}
                    </div>
                  </td>
                  <td className="muted">{formatTime(job.started_at)}</td>
                  <td className="muted">{formatTime(job.finished_at)}</td>
                  <td>
                    <div className="actions">
                      {job.has_report ? (
                        <button className="icon-btn" title="报告" onClick={() => setReport(job)}>
                          <FileText size={14} />
                        </button>
                      ) : null}
                      {active && job.cancellable ? (
                        <button className="icon-btn" title="取消" onClick={() => void cancel(job)}>
                          <Square size={14} />
                        </button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {report ? <ReportModal job={report} title={labelOf(report)} onClose={() => setReport(null)} /> : null}
      {openDoc ? <DocDrawer docId={openDoc} features={kb.features} onClose={() => setOpenDoc(null)} onChanged={refresh} /> : null}
    </div>
  );
}
