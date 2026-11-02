// 上传包编辑器的 iframe 宿主（ADR-0055）：不同源（sandbox 只给 allow-scripts，静态资源响应另有 CSP sandbox），
// 只经窄接口（packBridge）读写本包登记的产物、往对话发本包的命令、取主题变量；拿不到登录态，也发不了请求。
import { useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { errorText } from '../../api/errors.js';
import {
  errorResponse, event, handle, parseRequest, readTheme, response,
} from '../../app/packBridge.js';
import { Note } from '../../ui/index.jsx';

export default function PackFrame({ editor, artifact, types, readonly }) {
  const { api } = useApp();
  const { pid, subscribe } = useProject();
  const frame = useRef(null);
  const [session, setSession] = useState(null);
  const [error, setError] = useState(null);
  const ctx = useRef(null);
  ctx.current = {
    pack: editor.pack, version: session?.pack?.version || editor.version, label: session?.pack?.label || editor.pack,
    editor: { id: editor.id, title: editor.title }, artifact, types, readonly: !!readonly,
  };

  useEffect(() => {
    let live = true;
    setSession(null);
    setError(null);
    api.openPackUi(pid, editor.pack, editor.id).then((s) => { if (live) setSession(s); }).catch((e) => { if (live) setError(e); });
    return () => { live = false; };
  }, [api, pid, editor.pack, editor.id]);

  useEffect(() => {
    const deps = {
      readArtifact: (type, key) => api.artifact(pid, type, key),
      saveArtifact: (type, key, body) => api.saveArtifact(pid, type, key, body),
      sendCommand: ({ command, args, text }) => api.sendCommand(pid, { command, args, text, origin: { page: 'artifact', label: editor.title } }),
      theme: () => {
        const style = getComputedStyle(document.documentElement);
        return readTheme((name) => style.getPropertyValue(name));
      },
    };
    const onMessage = async (e) => {
      const win = frame.current?.contentWindow;
      if (!win || e.source !== win) return;
      let call;
      try {
        call = parseRequest(e.data);
      } catch (err) {
        if (err.id) win.postMessage(errorResponse(err.id, err), '*');
        return;
      }
      if (!call) return;
      try {
        win.postMessage(response(call.id, await handle(call, ctx.current, deps)), '*');
      } catch (err) {
        win.postMessage(errorResponse(call.id, err), '*');
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [api, pid, editor.title]);

  // 助手或别处改了这份产物：告诉编辑器，由它自己决定要不要重读
  useEffect(() => subscribe?.((env) => {
    if (env.type !== 'workspace.changed') return;
    if (!(env.data?.artifacts || []).some((a) => a.type === artifact.type && a.key === artifact.key)) return;
    frame.current?.contentWindow?.postMessage(event('artifact.changed', { type: artifact.type, key: artifact.key }), '*');
  }), [subscribe, artifact.type, artifact.key]);

  useEffect(() => {
    frame.current?.contentWindow?.postMessage(event('readonly.changed', { readonly: !!readonly }), '*');
  }, [readonly]);

  if (error) return <Note tone="red">{errorText(error)}</Note>;
  if (!session) return <div className="pk-load" role="status"><span className="spin" /></div>;
  return (
    <iframe
      ref={frame} className="pk-frame" src={session.url} title={editor.title}
      sandbox="allow-scripts" referrerPolicy="no-referrer" data-pack={editor.pack}
    />
  );
}
