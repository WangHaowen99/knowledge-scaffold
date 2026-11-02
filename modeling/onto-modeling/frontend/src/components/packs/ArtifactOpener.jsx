// 双击插件产物打开什么（ADR-0055）：按 AR-01 的 view.detail 选——包自带编辑器（官方包随平台编译、按包懒加载；
// 上传包进不同源的 iframe）、平台专用组件（去对应页面）、通用详情。包没启用时一行代码都不加载，只说「插件已停用」。
// artifact：{type, key, title?}；key 可以为空（由编辑器自己取第一项，如流程图页）。
import { Suspense, useEffect, useMemo, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { BUNDLED, bundledEditor } from '../../app/packEditors.js';
import { editorReadonly, openerState, packOfType } from '../../app/packUi.js';
import { errorText } from '../../api/errors.js';
import { Button, Icon, Modal } from '../../ui/index.jsx';
import ArtifactDetail from './ArtifactDetail.jsx';
import PackFrame from './PackFrame.jsx';
import '../../styles/packs.css';

function PluginOff({ type, onClose }) {
  const pack = packOfType(type);
  const go = () => { onClose?.(); navigate(pagePath('plugin-detail'), { pack }); };
  return (
    <Modal open onClose={onClose} icon="puzzle" title="插件已停用" w={420} foot={<Button kind="primary" onClick={go}>去启用</Button>}>
      <div className="sm muted pk-off" data-pack={pack}>启用后才能打开。</div>
    </Modal>
  );
}

function FrameDialog({ type, mode, artifact, types, onClose }) {
  // 写入范围随项目流 scope.updated 变；变了 PackFrame 给编辑器推 readonly.changed
  const { scope } = useProject();
  const readonly = editorReadonly(scope, type, artifact);
  return (
    <div className="overlay" onMouseDown={(e) => e.stopPropagation()}>
      <div className="pk-dlg" role="dialog" aria-modal="true" aria-label={artifact.title || mode.editor.title}>
        <div className="pk-h">
          <span className="ik"><Icon name="puzzle" size={16} /></span>
          <b className="grow ell">{artifact.title || artifact.key || type.title}</b>
          <span className="plg-tag"><Icon name="puzzle" size={12} />插件 · {mode.editor.title}</span>
          {readonly ? <span className="chip gray sm"><Icon name="lock" size={12} />只读</span> : null}
          <button type="button" className="icon-btn" title="关闭" aria-label="关闭" onClick={onClose}><Icon name="x" /></button>
        </div>
        <PackFrame editor={mode.editor} artifact={artifact} types={types} readonly={readonly} />
      </div>
    </div>
  );
}

function LoadError({ error, onRetry, onClose }) {
  return (
    <Modal open onClose={onClose} icon="alert" danger title="没打开" w={420} foot={<Button kind="primary" icon="refresh" onClick={onRetry}>重试</Button>}>
      <div className="sm muted">{errorText(error)}</div>
    </Modal>
  );
}

export default function ArtifactOpener({ artifact, onClose }) {
  const { api } = useApp();
  const { pid } = useProject();
  const [types, setTypes] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setError(null);
    api.artifactTypes(pid).then((r) => { if (live) setTypes(r.items || []); }).catch((e) => { if (live) setError(e); });
    return () => { live = false; };
  }, [api, pid, attempt]);

  const type = types ? types.find((t) => t.type === artifact.type) || null : undefined;
  const mode = useMemo(() => openerState({ types, error, type: artifact.type, bundled: BUNDLED }), [types, error, artifact.type]);
  // 平台专用组件在自己的页面里（如求解契约在代码生成页）：关掉弹窗、过去
  const page = mode?.kind === 'page' ? mode : null;
  useEffect(() => {
    if (!page) return;
    onClose?.();
    navigate(pagePath(page.page, pid), page.query);
  }, [page, pid, onClose]);

  if (mode.kind === 'error') return <LoadError error={mode.error} onRetry={() => setAttempt((n) => n + 1)} onClose={onClose} />;
  if (mode.kind === 'loading' || mode.kind === 'page') return null;
  if (mode.kind === 'disabled') return <PluginOff type={artifact.type} onClose={onClose} />;
  if (mode.kind === 'bundled') {
    const Editor = bundledEditor(mode.key);
    return <Suspense fallback={null}><Editor artifact={artifact} onClose={onClose} /></Suspense>;
  }
  if (mode.kind === 'iframe') {
    const own = types.filter((t) => t.pack === mode.editor.pack).map((t) => t.type);
    return <FrameDialog type={type} mode={mode} artifact={artifact} types={own} onClose={onClose} />;
  }
  return artifact.key ? <ArtifactDetail type={type} artifact={artifact} onClose={onClose} /> : null;
}
