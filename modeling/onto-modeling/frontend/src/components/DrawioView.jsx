import {
  ArrowLeftOutlined, DownloadOutlined, EditOutlined,
  FullscreenExitOutlined, FullscreenOutlined, ReloadOutlined, SaveOutlined,
} from '@ant-design/icons';
import { Alert, Button, Modal, Space, Spin, Tag, Tooltip, message } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { setWorkspaceDirty } from '../lib/workspaceDirty.js';
import DrawioPreview from './DrawioPreview.jsx';

const EDITOR_URL = '/drawio/index.html?embed=1&proto=json&configure=1&lang=zh&ui=min&libraries=1&offline=1&local=1&noExitBtn=1&pv=0';
const LOAD_TIMEOUT_MS = 20000;
const SAVE_LABEL = {
  clean: { text: '已保存', color: 'success' },
  dirty: { text: '有未保存修改', color: 'warning' },
  saving: { text: '保存中…', color: 'processing' },
  error: { text: '保存失败', color: 'error' },
};

// A new document gets new refs, pending requests and an independent preview/editor lifecycle.
export default function DrawioView(props) {
  return <DrawioDocument key={`${props.project.id}:${props.path}`} {...props} />;
}

function DrawioDocument({ project, path, editorAvailable, onSaved, onStateChange, onEditModeChange, refreshKey }) {
  const [modal, modalContext] = Modal.useModal();
  const [messageApi, messageContext] = message.useMessage();
  const [doc, setDoc] = useState(null);
  const [mode, setMode] = useState('view');
  const [saveState, setSaveState] = useState('clean');
  const [fullscreen, setFullscreen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [editorState, setEditorState] = useState('loading');
  const iframeRef = useRef(null);
  const latestXmlRef = useRef('');
  const revisionRef = useRef('');

  useEffect(() => {
    setWorkspaceDirty('drawio-editor', saveState === 'dirty' || saveState === 'saving' || saveState === 'error');
    onStateChange?.(saveState);
  }, [saveState, onStateChange]);
  useEffect(() => () => {
    setWorkspaceDirty('drawio-editor', false);
    onStateChange?.('clean');
    onEditModeChange?.(false);
  }, [onStateChange, onEditModeChange]);
  const dirtyRef = useRef(false);
  const savingRef = useRef(false);
  const aliveRef = useRef(true);
  const handlersRef = useRef({});
  const exitRequestedRef = useRef(false);
  const filename = path.split('/').pop();
  const parentPath = path.split('/').slice(0, -1).join(' / ');

  const acceptDocument = (data) => {
    if (data.truncated) throw new Error('流程图超过预览大小限制，请下载完整文件编辑。');
    const parsed = new DOMParser().parseFromString(data.content, 'text/xml');
    if (parsed.querySelector('parsererror') || !['mxfile', 'mxGraphModel'].includes(parsed.documentElement?.nodeName)) {
      throw new Error('流程图文件格式不完整或无效，请修复文件后重试。');
    }
    revisionRef.current = data.revision;
    latestXmlRef.current = data.content;
    setDoc(data);
  };

  useEffect(() => {
    // Assistant updates refresh the preview, but must never replace an active edit.
    if (mode !== 'view') return;
    let active = true;
    api.readFile(project.id, path).then((data) => active && acceptDocument(data))
      .catch((error) => active && setDoc({ error: error.message }));
    return () => { active = false; };
  }, [project.id, path, mode, refreshKey, attempt]);

  useEffect(() => {
    aliveRef.current = true;
    const guard = (event) => {
      if (dirtyRef.current || savingRef.current) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', guard);
    return () => {
      aliveRef.current = false;
      window.removeEventListener('beforeunload', guard);
    };
  }, []);

  const postToEditor = (payload) => {
    iframeRef.current?.contentWindow?.postMessage(JSON.stringify(payload), window.location.origin);
  };

  const requestSave = () => {
    if (savingRef.current || editorState !== 'ready') return;
    // The native save action commits an active label edit before serializing XML.
    postToEditor({ action: 'invokeAction', actionName: 'save' });
  };

  const requestLeave = () => {
    if (editorState !== 'ready') { leaveEditor(); return; }
    // Finish active label input before checking dirty state. The textContent reply
    // follows autosave notifications from resetEditor on the same message channel.
    exitRequestedRef.current = true;
    postToEditor({ action: 'resetEditor' });
    postToEditor({ action: 'textContent' });
  };

  const leaveEditor = (force = false) => {
    if (savingRef.current) return;
    if (!force && dirtyRef.current) {
      modal.confirm({
        title: '有未保存的修改',
        content: '返回预览将丢弃这些修改。可取消后在编辑器中保存。',
        okText: '放弃修改', cancelText: '继续编辑', okButtonProps: { danger: true },
        onOk: () => leaveEditor(true),
      });
      return;
    }
    dirtyRef.current = false;
    onEditModeChange?.(false);
    setMode('view');
    setSaveState('clean');
  };

  const persist = async (xml, { exitAfter = false } = {}) => {
    if (savingRef.current || typeof xml !== 'string') return;
    savingRef.current = true;
    latestXmlRef.current = xml;
    dirtyRef.current = true;
    setSaveState('saving');
    try {
      const result = await api.writeFile(project.id, path, xml, revisionRef.current);
      if (!aliveRef.current) return;
      revisionRef.current = result.revision;
      const stillDirty = latestXmlRef.current !== xml;
      dirtyRef.current = stillDirty;
      setDoc({ content: xml, revision: result.revision });
      setSaveState(stillDirty ? 'dirty' : 'clean');
      postToEditor({ action: 'status', message: stillDirty ? '仍有未保存修改' : '已保存', modified: stillDirty });
      onSaved?.();
      savingRef.current = false;
      if (exitAfter && !stillDirty) leaveEditor(true);
    } catch (error) {
      if (!aliveRef.current) return;
      setSaveState('error');
      if (error.status === 409) {
        modal.confirm({
          title: '流程图已被他人或助手修改',
          content: '当前编辑内容已保留。可继续编辑，或放弃本次修改并载入最新版本。',
          okText: '载入最新版本', cancelText: '继续编辑',
          onOk: async () => {
            try {
              acceptDocument(await api.readFile(project.id, path));
              dirtyRef.current = false;
              setSaveState('clean');
              setAttempt((value) => value + 1);
            } catch (reloadError) { messageApi.error(reloadError.message); }
          },
        });
      } else messageApi.error(error.message);
      postToEditor({ action: 'status', message: '保存失败，请重试', modified: true });
    } finally { savingRef.current = false; }
  };
  handlersRef.current = { persist, leaveEditor };

  useEffect(() => {
    if (mode !== 'edit') return;
    const iframe = iframeRef.current;
    let active = true;
    let initialized = false;
    let loaded = false;
    let timer;
    setEditorState('loading');
    const armTimeout = () => {
      clearTimeout(timer);
      timer = setTimeout(() => active && setEditorState('timeout'), LOAD_TIMEOUT_MS);
    };
    const onMessage = (event) => {
      if (!active || event.source !== iframe.contentWindow || event.origin !== window.location.origin) return;
      let data;
      try { data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data; }
      catch { return; }
      if (!data || typeof data !== 'object') return;
      if (data.event === 'configure') {
        postToEditor({ action: 'configure', config: {
          defaultFonts: ['Helvetica', 'Arial', 'sans-serif'],
        } });
      } else if (data.event === 'init' && !initialized) {
        initialized = true;
        armTimeout();
        postToEditor({ action: 'load', xml: latestXmlRef.current, title: filename,
          autosave: 1, fit: 1, border: 32, maxFitScale: 1 });
      } else if (data.event === 'load' && initialized) {
        clearTimeout(timer);
        if (data.error) { setEditorState('error'); return; }
        loaded = true;
        setEditorState('ready');
        // Fit around the diagram rather than the draw.io page origin. Small diagrams
        // stay at actual size; large diagrams shrink to the available viewport.
        const bounds = data.modelBounds;
        const size = data.containerSize;
        if (bounds?.width > 0 && bounds?.height > 0 && size?.width > 0 && size?.height > 0) {
          const width = Math.max(bounds.width + 64, size.width);
          const height = Math.max(bounds.height + 64, size.height);
          postToEditor({ action: 'viewbox', viewbox: {
            x: bounds.x + bounds.width / 2 - width / 2,
            y: bounds.y + bounds.height / 2 - height / 2, width, height, border: 0,
          } });
        }
      } else if (data.event === 'autosave' && loaded && typeof data.xml === 'string') {
        latestXmlRef.current = data.xml;
        dirtyRef.current = true;
        setSaveState(savingRef.current ? 'saving' : 'dirty');
      } else if (data.event === 'save' && loaded) {
        handlersRef.current.persist(data.xml, { exitAfter: Boolean(data.exit) });
      } else if (data.event === 'textContent' && exitRequestedRef.current) {
        exitRequestedRef.current = false;
        handlersRef.current.leaveEditor();
      } else if (data.event === 'exit') handlersRef.current.leaveEditor();
    };
    window.addEventListener('message', onMessage);
    armTimeout();
    // Navigate only AFTER installing the listener: cached editor resources may initialize immediately.
    iframe.src = EDITOR_URL;
    return () => {
      active = false;
      clearTimeout(timer);
      window.removeEventListener('message', onMessage);
    };
  }, [mode, attempt, filename]);

  const retry = () => setAttempt((value) => value + 1);
  const badge = SAVE_LABEL[saveState];
  return (
    <div className={`drawio-root${fullscreen ? ' fullscreen' : ''}`}>
      {modalContext}
      {messageContext}
      <div className="drawio-header">
        <div className="drawio-document-name">
          <strong title={filename}>{filename}</strong>
          <span title={parentPath}>{parentPath}</span>
        </div>
        <Space size={6} wrap className="drawio-actions">
          {mode === 'edit' && <Tag color={badge.color}>{badge.text}</Tag>}
          {mode === 'view' ? <>
            <Tooltip title={editorAvailable ? '在画布上编辑节点、文字和连线' : '当前部署未包含编辑器组件'}>
              <Button size="small" type="primary" icon={<EditOutlined />}
                disabled={!editorAvailable || !doc || !!doc.error} onClick={() => {
                  onEditModeChange?.(true);
                  setMode('edit');
                }}>编辑流程图</Button>
            </Tooltip>
            <Tooltip title="刷新预览"><Button size="small" aria-label="刷新预览" icon={<ReloadOutlined />} onClick={retry} /></Tooltip>
            <Tooltip title="下载原文件"><Button size="small" aria-label="下载原文件" icon={<DownloadOutlined />} href={api.rawUrl(project.id, path)} download={filename} /></Tooltip>
          </> : <>
            <Button size="small" type="primary" icon={<SaveOutlined />} loading={saveState === 'saving'}
              disabled={editorState !== 'ready'} onClick={requestSave}>保存</Button>
            <Button size="small" icon={<ArrowLeftOutlined />} disabled={saveState === 'saving'} onClick={requestLeave}>返回预览</Button>
          </>}
          <Tooltip title={fullscreen ? '退出全屏' : '全屏'}><Button size="small"
            aria-label={fullscreen ? '退出全屏' : '全屏'} icon={fullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
            onClick={() => setFullscreen(!fullscreen)} /></Tooltip>
        </Space>
      </div>
      {doc?.error ? <Alert type="error" showIcon message={doc.error} action={<Button onClick={retry}>重试</Button>} />
        : !doc ? <div className="drawio-loading-inline"><Spin />正在读取流程图…</div>
        : mode === 'view' ? <DrawioPreview xml={doc.content} /> : <>
          <div className="drawio-editor-shell">
            <iframe key={attempt} ref={iframeRef} className="drawio-editor" title={`编辑 ${filename}`}
              onError={() => setEditorState('error')} />
            {editorState !== 'ready' && <div className="drawio-loading" role="status">
              {editorState === 'loading' ? <><Spin />正在加载流程图编辑器…</> : <Alert type="warning" showIcon
                message={editorState === 'timeout' ? '编辑器加载超时' : '编辑器加载失败'}
                description="可以重试，或返回预览。尚未保存的修改会保留在本次编辑中。"
                action={<Button onClick={retry}>重新加载</Button>} />}
            </div>}
          </div>
          <div className="drawio-hint">双击节点修改文字 · 拖动节点调整位置 · Ctrl+S 保存</div>
        </>}
    </div>
  );
}
