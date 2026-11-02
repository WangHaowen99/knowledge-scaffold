// 上传包编辑器的 iframe 宿主（ADR-0055）：不同源（sandbox 只给 allow-scripts，静态资源响应另有 CSP sandbox），
// 只经窄接口（packBridge）读写本包登记的产物、往对话发本包的命令、取主题变量；拿不到登录态。
// 桥接走握手时转交的 MessagePort；iframe 第二次 load 说明它把自己导航走了，立即拆掉桥接、卸掉 iframe（审查第 2 条）。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import { createBridgeHost, handle, readTheme } from '../../app/packBridge.js';
import { Button, Note } from '../../ui/index.jsx';

// 要求 iframe 里的文档遵守的 CSP（Chromium 的 CSP 嵌入强制）：导航到别处的页面要么被拦，要么也只能用平台上包界面的
// 静态资源、发不了请求。来源写平台的完整地址而不是 'self'，免得外站页面的 'self' 变成它自己
export function requiredCsp(origin) {
  const base = `${origin}/api/v1/pack-ui/`;
  return [
    "default-src 'none'", `script-src ${base}`, `style-src ${base} 'unsafe-inline'`, `img-src ${base} data: blob:`,
    `font-src ${base}`, "connect-src 'none'", "form-action 'none'", "base-uri 'none'",
  ].join('; ');
}

export default function PackFrame({ editor, artifact, types, readonly }) {
  const { api } = useApp();
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
    editor: { id: editor.id, title: editor.title }, artifact, types, readonly: !!readonly,
    commands: session?.commands || [], state: limits.current,
  };

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
      readArtifact: (type, key) => api.artifact(pid, type, key),
      saveArtifact: (type, key, body) => api.saveArtifact(pid, type, key, body),
      // origin.pack：服务端据此按包再校验一次（只认本包的命令、参数短、限频）
      sendCommand: ({ command, args, text }) => api.sendCommand(pid, { command, args, text, origin: { page: 'artifact', label: editor.title, pack: editor.pack } }),
      theme: () => {
        const style = getComputedStyle(document.documentElement);
        return readTheme((name) => style.getPropertyValue(name));
      },
    };
    return createBridgeHost({
      target: () => frame.current?.contentWindow || null,
      respond: (call) => handle(call, ctx.current, deps),
      onNavigated: () => setLeft(true),
    });
  }, [session, api, pid, editor.title, editor.pack]);

  useEffect(() => {
    if (!host) return undefined;
    const onMessage = (e) => host.onWindowMessage(e);
    window.addEventListener('message', onMessage);
    return () => { window.removeEventListener('message', onMessage); host.close(); };
  }, [host]);

  // 助手或别处改了这份产物：告诉编辑器，由它自己决定要不要重读
  useEffect(() => subscribe?.((env) => {
    if (env.type !== 'workspace.changed') return;
    if (!(env.data?.artifacts || []).some((a) => a.type === artifact.type && a.key === artifact.key)) return;
    host?.send('artifact.changed', { type: artifact.type, key: artifact.key });
  }), [subscribe, host, artifact.type, artifact.key]);

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
      ref={frame} className="pk-frame" src={session.url} title={editor.title}
      sandbox="allow-scripts" referrerPolicy="no-referrer" data-pack={editor.pack}
      csp={requiredCsp(window.location.origin)} onLoad={() => host?.onLoad()}
    />
  );
}
