// 上传包编辑器的 iframe 宿主（ADR-0055）：不同源（sandbox 只给 allow-scripts，静态资源响应另有 CSP sandbox），
// 只经窄接口（packBridge）读写本包登记的产物、往对话发本包的命令、取主题变量；拿不到登录态。
// 桥接走握手时转交的 MessagePort；iframe 第二次 load 说明它把自己导航走了，立即拆掉桥接、卸掉 iframe（审查第 2 条）。
// 页面模式（ADR-0105）：插件自己的页面也用它，page 有值、artifact 为空；产物类型、上传位以 PL-20 返回的为准。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { artifactQuery } from '../../app/packUi.js';
import { errorText } from '../../api/errors.js';
import { createBridgeHost, handle, readTheme } from '../../app/packBridge.js';
import { navigate, pagePath } from '../../app/router.js';
import { Button, Note } from '../../ui/index.jsx';
import '../../styles/packs.css';

// 要求 iframe 里的文档遵守的 CSP（Chromium 的 CSP 嵌入强制）：导航到别处的页面要么被拦，要么也只能用平台上包界面的
// 静态资源、发不了请求。来源写平台的完整地址而不是 'self'，免得外站页面的 'self' 变成它自己
export function requiredCsp(origin) {
  const base = `${origin}/api/v1/pack-ui/`;
  return [
    "default-src 'none'", `script-src ${base}`, `style-src ${base} 'unsafe-inline'`, `img-src ${base} data: blob:`,
    `font-src ${base}`, "connect-src 'none'", "form-action 'none'", "base-uri 'none'",
  ].join('; ');
}

// 给页面的命令任务只带这几样：不把对话内容、结果引用交给插件
function taskView(task) {
  return { task_id: task.task_id, state: task.state, title: task.title || '', percent: task.percent ?? null };
}

export default function PackFrame({ editor, artifact = null, page = null, types, readonly }) {
  const { api, services } = useApp();
  const { pid, subscribe } = useProject();
  const frame = useRef(null);
  const [session, setSession] = useState(null);
  const [error, setError] = useState(null);
  const [left, setLeft] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const ctx = useRef(null);
  const limits = useRef({});
  ctx.current = {
    pack: editor.pack, version: session?.pack?.version || editor.version, label: session?.pack?.label || editor.pack,
    editor: { id: editor.id, title: editor.title }, artifact, page, types: types || session?.types || [],
    readonly: !!readonly, commands: session?.commands || [], uploads: session?.uploads || [], state: limits.current,
  };
  // 本页发出的命令记在这个来源下（S-19 按它查还在跑的任务，项目流 command.progress 按它转给页面）
  const originPage = page ? page.key : 'artifact';
  const pageTitle = page?.title || '';

  useEffect(() => {
    let live = true;
    setSession(null);
    setError(null);
    setLeft(false);
    api.openPackUi(pid, editor.pack, editor.id).then((s) => { if (live) setSession(s); }).catch((e) => { if (live) setError(e); });
    return () => { live = false; };
  }, [api, pid, editor.pack, editor.id, attempt]);

  // 每次装上 iframe 建一个新的桥接宿主（重新打开时也是新的）
  const host = useMemo(() => {
    if (!session) return null;
    const deps = {
      readArtifact: (type, key) => api.artifact(pid, type, key, artifactQuery(ctx.current.artifact, type, key)),
      saveArtifact: (type, key, body) => api.saveArtifact(pid, type, key, body),
      // origin.pack：服务端据此按包再校验一次（只认本包的命令、参数短、限频）
      sendCommand: ({ command, args, text }) => api.sendCommand(pid, { command, args, text, origin: { page: originPage, label: pageTitle || editor.title, pack: editor.pack } }),
      listArtifacts: (type, query) => api.artifacts(pid, type, query),
      listUploads: (slot) => api.packSlotUploads(pid, editor.pack, slot),
      putUpload: async (slot, name, data) => {
        const item = await api.putPackSlotUpload(pid, editor.pack, slot, new Blob([data]), name);
        host?.send('upload.changed', { slot });
        return item;
      },
      deleteUpload: async (slot, id) => {
        const out = await api.deletePackSlotUpload(pid, editor.pack, slot, id);
        host?.send('upload.changed', { slot });
        return out;
      },
      download: (path) => services.download(`/projects/${encodeURIComponent(pid)}/packs/${encodeURIComponent(editor.pack)}/files`,
        (path.split('/').pop() || editor.pack), { query: { path } }),
      openStudio: (kind, key) => navigate(pagePath('studio', pid), { entry: `${kind}:${key}` }),
      commandTasks: async () => {
        const r = await api.commandTasks(pid, { origin_page: originPage });
        return { items: (r.items || []).map(taskView) };
      },
      theme: () => {
        const style = getComputedStyle(document.documentElement);
        return readTheme((name) => style.getPropertyValue(name));
      },
    };
    // deps 里的上传回调在调用时（宿主已建好）才用到 host
    const host = createBridgeHost({
      target: () => frame.current?.contentWindow || null,
      respond: (call) => handle(call, ctx.current, deps),
      onNavigated: () => setLeft(true),
    });
    return host;
    // 依赖只放不变的值：宿主一重建，旧端口就关了，iframe 只握手一次，收不到后面的回应（页面对象每次渲染都是新的）
  }, [session, api, services, pid, editor.title, editor.pack, pageTitle, originPage]);

  useEffect(() => {
    if (!host) return undefined;
    const onMessage = (e) => host.onWindowMessage(e);
    window.addEventListener('message', onMessage);
    return () => { window.removeEventListener('message', onMessage); host.close(); };
  }, [host]);

  // 助手或别处改了产物：告诉编辑器（页面模式是本包的任一产物），由它自己决定要不要重读；
  // 页面模式另转本页发出的命令的进度
  useEffect(() => subscribe?.((env) => {
    if (env.type === 'command.progress') {
      if (page && env.data?.origin?.page === originPage) host?.send('command.progress', taskView(env.data));
      return;
    }
    if (env.type !== 'workspace.changed') return;
    const changed = (env.data?.artifacts || []).filter((a) => (artifact
      ? a.type === artifact.type && a.key === artifact.key
      : ctx.current.types.includes(a.type)));
    changed.slice(0, 50).forEach((a) => host?.send('artifact.changed', { type: a.type, key: a.key }));
  }), [subscribe, host, artifact?.type, artifact?.key, page?.key, originPage]);

  useEffect(() => { host?.send('readonly.changed', { readonly: !!readonly }); }, [host, readonly]);

  const reopen = useCallback(() => setAttempt((n) => n + 1), []);

  if (left) {
    return (
      <div className="pk-load pk-left" role="alert">
        <Note tone="amber">这个编辑器跳到了别的页面，已经关掉。<Button size="xs" kind="ghost" icon="refresh" onClick={reopen}>重新打开</Button></Note>
      </div>
    );
  }
  if (error) return <Note tone="red">{errorText(error)}</Note>;
  if (!session) return <div className="pk-load" role="status"><span className="spin" /></div>;
  return (
    <iframe
      ref={frame} className={page ? 'pk-frame pk-page' : 'pk-frame'} src={session.url} title={page?.title || editor.title}
      sandbox="allow-scripts" referrerPolicy="no-referrer" data-pack={editor.pack}
      csp={requiredCsp(window.location.origin)} onLoad={() => host?.onLoad()}
    />
  );
}
