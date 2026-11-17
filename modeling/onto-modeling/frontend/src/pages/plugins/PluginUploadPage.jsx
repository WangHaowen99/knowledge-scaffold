// 上传与审核（设计稿 plugin-upload；PL-13～PL-18）。普通用户同一页装自己的包（PL-22～PL-26，ADR-0079）：
// 看到的只有自己的上传，右栏换成「安装」，装好后只有自己能启用。
// 左栏上传区与上传列表、静态检查；右栏发布到通道（灰度、熔断）与审核记录。选中哪个上传，检查、发布、记录都跟着它。
// 静态检查在上传响应之后跑（PL-13 返回 checking），有检查中的上传时每 2.5 秒刷新列表。
// 地址参数 ?upload=<上传 ID> 直接选中那一个。
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead, ShellG } from '../../shell/Shell.jsx';
import { can } from '../../state/roles.js';
import { Note } from '../../ui/index.jsx';
import '../../styles/plugins-upload.css';
import { PluginsNav } from './parts.jsx';
import {
  eligibleProjects, isPublishable, packLimit, pickUpload, publishBody, publishToast, uploadErrorText, uploadTitle, validatePackFile,
} from './uploadModel.js';
import {
  CheckCard, InstallCard, PublishCard, RejectModal, TimelineCard, UploadCard, useActorNames,
} from './uploadParts.jsx';

const POLL_MS = 2500;

export default function PluginUploadPage({ route }) {
  const { user } = useApp();
  return <UploadReview route={route} mine={!can(user, 'admin')} />;
}

// 灰度范围的项目数与可指定的项目：O-01 的项目元数据（admin 只读运维面）；取不到时只写百分比
function useRolloutProjects(active) {
  const { api } = useApp();
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!active || data) return undefined;
    let alive = true;
    api.ops.overview({ range: 'today' })
      .then((r) => alive && setData({
        total: eligibleProjects(r.stats),
        projects: (r.projects || []).filter((p) => !p.archived).map((p) => ({ id: p.id, label: p.name })),
      }))
      .catch(() => alive && setData({ total: null, projects: [] }));
    return () => { alive = false; };
  }, [api, active, data]);
  return data;
}

function UploadReview({ route, mine }) {
  const { api, services, config } = useApp();
  const [list, setList] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [wanted, setWanted] = useState(route.query?.upload || null);
  const [detail, setDetail] = useState(null);
  const [tick, setTick] = useState(0);
  const [sending, setSending] = useState([]);
  const [busy, setBusy] = useState(false);
  const [publishError, setPublishError] = useState(null);
  const [rejecting, setRejecting] = useState(null);

  const loadList = useCallback(async () => {
    try {
      const r = await (mine ? api.myPackUploads : api.packUploads)({ limit: 50 });
      setList(r.items || []);
      setLoadError(null);
    } catch (e) {
      setLoadError(errorText(e));
      setList((l) => l || []);
    }
  }, [api, mine]);
  useEffect(() => { loadList(); }, [loadList]);

  const selectedId = useMemo(() => (list ? pickUpload(list, wanted) : null), [list, wanted]);
  const selectedState = list?.find((u) => u.id === selectedId)?.state;

  // 选中的上传的详情（PL-15）：换了选中、状态变了、做完操作后重取
  useEffect(() => { setPublishError(null); }, [selectedId]);
  useEffect(() => {
    if (!selectedId) { setDetail(null); return undefined; }
    let alive = true;
    (mine ? api.myPackUpload : api.packUpload)(selectedId).then((d) => alive && setDetail(d)).catch((e) => alive && toast(errorText(e), 'alert'));
    return () => { alive = false; };
  }, [api, mine, selectedId, selectedState, tick]);

  // 有检查中的上传时轮询列表（检查跑完没有事件）
  const checking = !!list?.some((u) => u.state === 'checking');
  useEffect(() => {
    if (!checking) return undefined;
    const t = setInterval(loadList, POLL_MS);
    return () => clearInterval(t);
  }, [checking, loadList]);

  const refresh = async () => { await loadList(); setTick((n) => n + 1); };

  const limit = packLimit(config, mine);
  const patchSending = (key, patch) => setSending((s) => s.map((x) => (x.key === key ? { ...x, ...patch } : x)));
  const onFiles = async (files) => {
    for (const file of files) {
      const key = `${file.name}:${Date.now()}:${Math.random().toString(16).slice(2, 6)}`;
      const problem = validatePackFile(file, limit);
      setSending((s) => [...s, { key, name: file.name, size: file.size, pct: 0, error: problem }]);
      if (problem) continue;
      try {
        const rec = await services.uploadPack(file, { mine, onProgress: (pct) => patchSending(key, { pct }) });
        setSending((s) => s.filter((x) => x.key !== key));
        setWanted(rec.id);
        await loadList();
        toast(`已上传 ${file.name}，正在做静态检查`, 'upload');
      } catch (e) {
        patchSending(key, { error: uploadErrorText(e) });
      }
    }
  };

  const u = detail && detail.id === selectedId ? detail : null;
  const rollout = useRolloutProjects(!mine && isPublishable(u));
  const nameOf = useActorNames((u?.timeline || []).map((x) => x.actor));

  const publish = async (form, target) => {
    setBusy(true);
    setPublishError(null);
    setWanted(target.id);
    try {
      await api.publishPackUpload(target.id, publishBody(form));
      toast(publishToast(target, form), 'upload');
    } catch (e) {
      setPublishError(uploadErrorText(e, target));
    } finally {
      setBusy(false);
      await refresh();
    }
  };

  const install = async (target) => {
    setBusy(true);
    setPublishError(null);
    setWanted(target.id);
    try {
      await api.installMyPackUpload(target.id);
      toast(`已安装 ${uploadTitle(target)}`, 'upload');
    } catch (e) {
      setPublishError(uploadErrorText(e, target));
    } finally {
      setBusy(false);
      await refresh();
    }
  };

  const reject = async (reason) => {
    setBusy(true);
    setWanted(rejecting.id);
    try {
      await api.rejectPackUpload(rejecting.id, { reason });
      toast(`已退回 ${uploadTitle(rejecting)}`, 'undo');
      setRejecting(null);
    } catch (e) {
      toast(uploadErrorText(e, rejecting), 'alert');
    } finally {
      setBusy(false);
      await refresh();
    }
  };

  const download = (x) => services.download(`${mine ? '/me/pack-uploads' : '/admin/packs/uploads'}/${encodeURIComponent(x.id)}/report`, `${x.id}-report.md`)
    .catch((e) => toast(errorText(e), 'alert'));

  return (
    <ShellG active="plugins" side={<PluginsNav active="up" />}>
      <PageHead title={mine ? '安装插件' : '上传与审核'} />
      {loadError ? (
        <Note tone="red" className="mb12">上传列表没读到：{loadError} <a className="link" onClick={loadList}>重试</a></Note>
      ) : null}
      {list ? (
        <div className="plu-grid">
          <div className="col" style={{ gap: 16 }}>
            <UploadCard
              list={list} sending={sending} selectedId={selectedId} limit={limit} mine={mine}
              onFiles={onFiles} onSelect={setWanted} onDismiss={(key) => setSending((s) => s.filter((x) => x.key !== key))}
            />
            <CheckCard list={list} selectedId={selectedId} detail={u} onSelect={setWanted} onDownload={download} onReject={mine ? null : setRejecting} />
          </div>
          <div className="col" style={{ gap: 16 }}>
            {mine ? (
              <InstallCard upload={u || list.find((x) => x.id === selectedId) || null} busy={busy} error={publishError} onInstall={install} />
            ) : (
              <PublishCard
                upload={u || list.find((x) => x.id === selectedId) || null} rollout={rollout} busy={busy} error={publishError}
                onPublish={publish}
                onHold={(x) => toast(`${uploadTitle(x)} 暂不发布`, 'pause')}
              />
            )}
            <TimelineCard upload={u} nameOf={nameOf} mine={mine} />
          </div>
        </div>
      ) : <div className="muted sm">正在读取上传列表…</div>}
      {rejecting ? <RejectModal upload={rejecting} busy={busy} onClose={() => setRejecting(null)} onSubmit={reject} /> : null}
    </ShellG>
  );
}
