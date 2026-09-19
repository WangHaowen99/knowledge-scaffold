import {
  CloseOutlined,
  DownloadOutlined,
  EditOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
} from '@ant-design/icons';
import { Alert, Button, Modal, Space, Spin, Tag, Tooltip, Typography, message } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';

// drawio 官方 webapp 由后端挂载在 /drawio（离线部署）；embed 协议：init→load→autosave/save→exit
const DRAWIO_EDITOR_URL =
  '/drawio/index.html?embed=1&proto=json&spin=1&lang=zh&ui=min&libraries=1';
const VIEWER_SCRIPT = '/drawio/js/viewer-static.min.js';

let viewerScriptPromise = null;
function loadViewerScript() {
  if (window.GraphViewer) return Promise.resolve(true);
  viewerScriptPromise =
    viewerScriptPromise ||
    new Promise((resolve) => {
      const script = document.createElement('script');
      script.src = VIEWER_SCRIPT;
      script.onload = () => resolve(true);
      script.onerror = () => resolve(false);
      document.head.appendChild(script);
    });
  return viewerScriptPromise;
}

function Viewer({ xml }) {
  const holderRef = useRef(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    loadViewerScript().then((ok) => {
      if (!active || !holderRef.current) return;
      if (!ok || !window.GraphViewer) {
        setFailed(true);
        return;
      }
      holderRef.current.innerHTML = '';
      const el = document.createElement('div');
      el.className = 'mxgraph';
      el.dataset.mxgraph = JSON.stringify({
        xml,
        toolbar: 'pages zoom layers lightbox',
        'toolbar-position': 'top',
        'toolbar-nohide': true,
        nav: true,
        resize: true,
        border: 8,
      });
      holderRef.current.appendChild(el);
      try {
        window.GraphViewer.createViewerForElement(el);
      } catch {
        setFailed(true);
      }
    });
    return () => {
      active = false;
    };
  }, [xml]);

  if (failed) {
    return <Alert type="warning" showIcon message="drawio 渲染组件不可用，请下载原文件查看" />;
  }
  return <div ref={holderRef} className="drawio-viewer" />;
}

const SAVE_LABEL = {
  clean: { text: '已保存', color: 'success' },
  dirty: { text: '有未保存修改', color: 'warning' },
  saving: { text: '保存中…', color: 'processing' },
  error: { text: '保存失败', color: 'error' },
};

export default function DrawioView({ project, path, editorAvailable, onSaved }) {
  const [doc, setDoc] = useState(null); // {content, revision} | {error}
  const [mode, setMode] = useState('view'); // view | edit
  const [saveState, setSaveState] = useState('clean');
  const [fullscreen, setFullscreen] = useState(false);
  const iframeRef = useRef(null);
  const revisionRef = useRef('');
  const latestXmlRef = useRef('');
  const dirtyRef = useRef(false);

  useEffect(() => {
    setMode('view');
    setSaveState('clean');
    setDoc(null);
    api
      .readFile(project.id, path)
      .then((data) => {
        revisionRef.current = data.revision;
        latestXmlRef.current = data.content;
        setDoc(data);
      })
      .catch((error) => setDoc({ error: error.message }));
  }, [project.id, path]);

  // 编辑期间意外关闭页面的守卫（人因学：不让未保存修改无声丢失）
  useEffect(() => {
    const guard = (event) => {
      if (mode === 'edit' && dirtyRef.current) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [mode]);

  const persist = async (xml, { exitAfter = false } = {}) => {
    setSaveState('saving');
    try {
      const result = await api.writeFile(project.id, path, xml, revisionRef.current);
      revisionRef.current = result.revision;
      latestXmlRef.current = xml;
      dirtyRef.current = false;
      setSaveState('clean');
      postToEditor({ action: 'status', message: '已保存并记录版本', modified: false });
      onSaved?.();
      if (exitAfter) leaveEditor(true);
    } catch (error) {
      if (error.status === 409) {
        setSaveState('dirty');
        resolveConflict(xml);
        return;
      }
      setSaveState('error');
      message.error(error.message);
      postToEditor({ action: 'status', message: `保存失败：${error.message}`, modified: true });
    }
  };

  // 与助手写同一文件撞车时，把选择权交给用户，不做任何静默覆盖
  const resolveConflict = (myXml) => {
    Modal.confirm({
      title: '流程图已被他人或助手修改',
      content:
        '在你编辑期间该文件产生了新版本。选择保留哪份内容（另一份仍可在「版本」时间线中找回）。',
      okText: '以我的内容覆盖保存',
      cancelText: '放弃我的修改，载入最新版',
      onOk: async () => {
        const latest = await api.readFile(project.id, path);
        revisionRef.current = latest.revision;
        await persist(myXml);
      },
      onCancel: async () => {
        const latest = await api.readFile(project.id, path);
        revisionRef.current = latest.revision;
        latestXmlRef.current = latest.content;
        dirtyRef.current = false;
        setSaveState('clean');
        postToEditor({ action: 'load', xml: latest.content, autosave: 1 });
      },
    });
  };

  const postToEditor = (payload) => {
    iframeRef.current?.contentWindow?.postMessage(JSON.stringify(payload), '*');
  };

  const leaveEditor = (force = false) => {
    if (!force && dirtyRef.current) {
      Modal.confirm({
        title: '有未保存的修改',
        content: '退出编辑将丢弃这些修改（在编辑器里按 Ctrl+S 可先保存）。',
        okText: '仍然退出',
        okButtonProps: { danger: true },
        cancelText: '继续编辑',
        onOk: () => leaveEditor(true),
      });
      return;
    }
    dirtyRef.current = false;
    setFullscreen(false);
    setMode('view');
    api
      .readFile(project.id, path)
      .then((data) => {
        revisionRef.current = data.revision;
        latestXmlRef.current = data.content;
        setDoc(data);
      })
      .catch(() => {});
  };

  useEffect(() => {
    if (mode !== 'edit') return undefined;
    const onMessage = (event) => {
      if (event.source !== iframeRef.current?.contentWindow) return;
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (data.event === 'init') {
        postToEditor({ action: 'load', xml: latestXmlRef.current || '', autosave: 1 });
        setSaveState('clean');
      } else if (data.event === 'autosave') {
        latestXmlRef.current = data.xml;
        dirtyRef.current = true;
        setSaveState('dirty');
      } else if (data.event === 'save') {
        persist(data.xml, { exitAfter: Boolean(data.exit) });
      } else if (data.event === 'exit') {
        leaveEditor();
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, project.id, path]);

  if (doc?.error) {
    return <Alert type="error" showIcon message={doc.error} />;
  }
  if (!doc) {
    return <Spin style={{ marginTop: 48 }} />;
  }

  const badge = SAVE_LABEL[saveState];

  return (
    <div className={`drawio-root${fullscreen ? ' fullscreen' : ''}`}>
      <Space style={{ marginBottom: 8 }} align="center" wrap>
        <Typography.Title level={5} style={{ margin: 0 }}>
          {path}
        </Typography.Title>
        {mode === 'edit' && <Tag color={badge.color}>{badge.text}</Tag>}
        {mode === 'view' ? (
          <>
            <Tooltip
              title={editorAvailable ? '' : '当前部署未包含 drawio 编辑器组件'}
            >
              <Button
                size="small"
                type="primary"
                icon={<EditOutlined />}
                disabled={!editorAvailable}
                onClick={() => setMode('edit')}
              >
                编辑流程图
              </Button>
            </Tooltip>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              href={api.rawUrl(project.id, path)}
            >
              下载原文件
            </Button>
          </>
        ) : (
          <>
            <Button
              size="small"
              icon={fullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
              onClick={() => setFullscreen(!fullscreen)}
            >
              {fullscreen ? '退出全屏' : '全屏编辑'}
            </Button>
            <Button size="small" icon={<CloseOutlined />} onClick={() => leaveEditor()}>
              完成编辑
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              编辑器内 Ctrl+S 保存并记录版本
            </Typography.Text>
          </>
        )}
      </Space>
      {mode === 'view' ? (
        <Viewer xml={doc.content} />
      ) : (
        <iframe
          ref={iframeRef}
          className="drawio-editor"
          title={`编辑 ${path}`}
          src={DRAWIO_EDITOR_URL}
        />
      )}
    </div>
  );
}
