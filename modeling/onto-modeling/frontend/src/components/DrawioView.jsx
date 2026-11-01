// drawio 编辑：官方 webapp 静态部署在 /drawio（后端挂载，缺席时只读降级），以 iframe embed 方式接入。
// DrawioEditorFrame 只管编辑器本身（按内容读写），流程图弹窗的「drawio 高级编辑」直接用它；
// 默认导出 DrawioView 按项目文件读写（F-02 / F-03），资料区的 .drawio 预览与编辑用它。
import {
  forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState,
} from 'react';
import { api } from '../api/files.js';
import { errorText } from '../api/errors.js';
import {
  Button, Chip, Icon, Modal, Note, cx,
} from '../ui/index.jsx';
import DrawioPreview from './DrawioPreview.jsx';
import '../styles/flowchart.css';

const EDITOR_PATH = '/drawio/index.html';
const EDITOR_URL = `${EDITOR_PATH}?embed=1&proto=json&configure=1&lang=zh&ui=min&libraries=1&offline=1&local=1&noExitBtn=1&pv=0`;
const LOAD_TIMEOUT_MS = 20000;

// 编辑器静态资源是否部署：先探一下，避免 iframe 里出现 404 页面再等超时
let probe = null;
function probeEditor() {
  if (!probe) {
    probe = window.fetch(EDITOR_PATH, { method: 'HEAD', cache: 'no-store' })
      .then((r) => r.ok && /html/.test(r.headers.get('Content-Type') || 'text/html'))
      .catch(() => false)
      .then((ok) => { if (!ok) setTimeout(() => { probe = null; }, 0); return ok; });
  }
  return probe;
}

/**
 * drawio 编辑器 iframe。
 * @param {object} p
 * @param {string} p.xml 打开时载入的 XML（之后的变化不会重载编辑器）
 * @param {(xml: string) => void} [p.onChange] 编辑器自动保存通知（有未保存修改）
 * @param {(xml: string) => Promise<void>} p.onSave 编辑器里按保存（Ctrl+S 或宿主调用 save()）
 * @param {(state: 'loading'|'ready'|'error'|'timeout'|'unavailable') => void} [p.onStatus]
 * @param {(state: string, retry: () => void) => React.ReactNode} [p.fallback] 没加载出来时显示什么
 * @param {boolean} [p.available] 部署里有没有编辑器（C-01 features.drawio）；false 时直接走 fallback
 */
export const DrawioEditorFrame = forwardRef(function DrawioEditorFrame({
  xml, title = '流程图', onChange, onSave, onStatus, fallback, className, available = true,
}, ref) {
  const iframeRef = useRef(null);
  const [state, setState] = useState('loading');
  const [attempt, setAttempt] = useState(0);
  const latest = useRef(xml);
  const handlers = useRef({});
  handlers.current = { onChange, onSave };

  const post = useCallback((payload) => {
    iframeRef.current?.contentWindow?.postMessage(JSON.stringify(payload), window.location.origin);
  }, []);

  useImperativeHandle(ref, () => ({
    ready: state === 'ready',
    // 让编辑器走自己的保存动作（会先提交正在输入的文字），结果经 onSave 回来
    save: () => { if (state === 'ready') post({ action: 'invokeAction', actionName: 'save' }); return state === 'ready'; },
    latestXml: () => latest.current,
  }), [state, post]);

  useEffect(() => { onStatus?.(state); }, [state]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let active = true;
    let timer;
    let initialized = false;
    let loaded = false;
    setState('loading');
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => active && !loaded && setState('timeout'), LOAD_TIMEOUT_MS);
    };
    const onMessage = async (event) => {
      const iframe = iframeRef.current;
      if (!active || !iframe || event.source !== iframe.contentWindow || event.origin !== window.location.origin) return;
      let data;
      try { data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data; } catch { return; }
      if (!data || typeof data !== 'object') return;
      if (data.event === 'configure') {
        post({ action: 'configure', config: { defaultFonts: ['PingFang SC', 'Helvetica', 'Arial', 'sans-serif'] } });
      } else if (data.event === 'init' && !initialized) {
        initialized = true;
        arm();
        post({ action: 'load', xml: latest.current, title, autosave: 1, fit: 1, border: 32, maxFitScale: 1 });
      } else if (data.event === 'load' && initialized) {
        clearTimeout(timer);
        if (data.error) { setState('error'); return; }
        loaded = true;
        setState('ready');
      } else if (data.event === 'autosave' && loaded && typeof data.xml === 'string') {
        latest.current = data.xml;
        handlers.current.onChange?.(data.xml);
      } else if (data.event === 'save' && loaded && typeof data.xml === 'string') {
        latest.current = data.xml;
        try {
          await handlers.current.onSave?.(data.xml);
          post({ action: 'status', message: '已保存', modified: false });
        } catch {
          post({ action: 'status', message: '保存失败，请重试', modified: true });
        }
      }
    };
    window.addEventListener('message', onMessage);
    (available ? probeEditor() : Promise.resolve(false)).then((ok) => {
      if (!active) return;
      if (!ok) { setState('unavailable'); return; }
      arm();
      // 先装好监听再导航：缓存命中时编辑器会立刻初始化
      if (iframeRef.current) iframeRef.current.src = EDITOR_URL;
    });
    return () => {
      active = false;
      clearTimeout(timer);
      window.removeEventListener('message', onMessage);
    };
  }, [attempt, post, title, available]);

  const retry = () => setAttempt((v) => v + 1);
  return (
    <div className={cx('dio-shell', className)}>
      {state !== 'unavailable' ? <iframe key={attempt} ref={iframeRef} className="dio-frame" title={`编辑 ${title}`} onError={() => setState('error')} /> : null}
      {state === 'loading' ? <div className="dio-mask" role="status"><span className="spin" />正在加载 drawio 编辑器…</div> : null}
      {['error', 'timeout', 'unavailable'].includes(state) ? (
        fallback ? fallback(state, retry) : (
          <div className="dio-mask">
            <Note tone="amber">
              {state === 'unavailable' ? '当前部署没有包含 drawio 编辑器组件，只能查看。' : state === 'timeout' ? 'drawio 编辑器加载超时。' : 'drawio 编辑器加载失败。'}
              {state !== 'unavailable' ? <> 尚未保存的修改会保留。<Button size="xs" kind="ghost" icon="refresh" onClick={retry}>重新加载</Button></> : null}
            </Note>
          </div>
        )
      ) : null}
    </div>
  );
});

export function drawioStateText(state) {
  return {
    loading: '正在加载编辑器…',
    unavailable: '当前部署没有 drawio 编辑器，只能查看',
    timeout: '编辑器加载超时',
    error: '编辑器加载失败',
  }[state] || '';
}

// 按文件读写：一个文件一套预览 / 编辑生命周期
export default function DrawioView(props) {
  return <DrawioDocument key={`${props.project.id}:${props.path}`} {...props} />;
}

const SAVE_CHIP = {
  clean: ['teal', 'check', '已保存'],
  dirty: ['amber', null, '有未保存修改'],
  saving: ['gray', null, '保存中…'],
  error: ['red', 'alert', '保存失败'],
};

function DrawioDocument({ project, path, editorAvailable = true, onSaved, refreshKey }) {
  const [doc, setDoc] = useState(null);
  const [mode, setMode] = useState('view');
  const [saveState, setSaveState] = useState('clean');
  const [fullscreen, setFullscreen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [ask, setAsk] = useState(null); // 'leave' | {conflict}
  const [editorReady, setEditorReady] = useState(false);
  const frame = useRef(null);
  const revision = useRef('');
  const dirty = useRef(false);
  const filename = path.split('/').pop();
  const parentPath = path.split('/').slice(0, -1).join(' / ');

  const accept = (data) => {
    if (data.truncated) throw new Error('流程图超过预览大小限制，请下载完整文件编辑。');
    const parsed = new DOMParser().parseFromString(data.content, 'text/xml');
    if (parsed.querySelector('parsererror') || !['mxfile', 'mxGraphModel'].includes(parsed.documentElement?.nodeName)) {
      throw new Error('流程图文件格式不完整或无效，请修复文件后重试。');
    }
    revision.current = data.revision;
    setDoc(data);
  };

  useEffect(() => {
    // 助手改了文件会刷新预览，但不打断正在进行的编辑
    if (mode !== 'view') return undefined;
    let alive = true;
    api.readFile(project.id, path)
      .then((data) => { if (alive) accept(data); })
      .catch((e) => alive && setDoc({ error: e.message || errorText(e) }));
    return () => { alive = false; };
  }, [project.id, path, mode, refreshKey, attempt]);

  useEffect(() => {
    const guard = (e) => { if (dirty.current) { e.preventDefault(); e.returnValue = ''; } };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, []);

  const leave = (force = false) => {
    if (!force && dirty.current) { setAsk('leave'); return; }
    dirty.current = false;
    setAsk(null);
    setMode('view');
    setSaveState('clean');
  };

  const persist = async (xml) => {
    setSaveState('saving');
    try {
      const r = await api.writeFile(project.id, path, xml, revision.current);
      revision.current = r.revision;
      dirty.current = false;
      setDoc({ content: xml, revision: r.revision });
      setSaveState('clean');
      onSaved?.();
    } catch (e) {
      setSaveState('error');
      if (e.status === 409) setAsk({ conflict: true });
      throw e;
    }
  };

  const chip = SAVE_CHIP[saveState];
  return (
    <div className={cx('dio-root', fullscreen && 'full')}>
      <div className="dio-head">
        <div className="dio-name"><b className="ell" title={filename}>{filename}</b><span className="ell" title={parentPath}>{parentPath}</span></div>
        <span className="sp" />
        {mode === 'edit' ? <Chip tone={chip[0]} icon={chip[1]} dot={!chip[1]}>{chip[2]}</Chip> : null}
        {mode === 'view' ? (
          <>
            <Button
              size="sm" kind="primary" icon="edit" disabled={!editorAvailable || !doc || !!doc.error}
              title={editorAvailable ? '在画布上编辑节点、文字和连线' : '当前部署没有包含 drawio 编辑器组件'}
              onClick={() => { setEditorReady(false); setMode('edit'); }}
            >
              编辑流程图
            </Button>
            <Button size="sm" kind="ghost" icon="refresh" title="刷新预览" onClick={() => setAttempt((v) => v + 1)} />
            <a className="btn sm ghost icon" href={api.rawUrl(project.id, path)} title="下载原文件" download={filename}><Icon name="download" size={14} /></a>
          </>
        ) : (
          <>
            <Button size="sm" kind="primary" icon="check" disabled={!editorReady || saveState === 'saving'} onClick={() => frame.current?.save()}>保存</Button>
            <Button size="sm" icon="arrowl" disabled={saveState === 'saving'} onClick={() => leave()}>返回预览</Button>
          </>
        )}
        <Button size="sm" kind="ghost" icon="maximize" title={fullscreen ? '退出全屏' : '全屏'} onClick={() => setFullscreen((v) => !v)} />
      </div>
      {doc?.error ? (
        <div style={{ padding: 16 }}><Note tone="red">{doc.error} <Button size="xs" kind="ghost" icon="refresh" onClick={() => setAttempt((v) => v + 1)}>重试</Button></Note></div>
      ) : !doc ? (
        <div className="dio-mask static" role="status"><span className="spin" />正在读取流程图…</div>
      ) : mode === 'view' ? (
        <DrawioPreview xml={doc.content} />
      ) : (
        <>
          <DrawioEditorFrame
            ref={frame} xml={doc.content} title={filename} available={editorAvailable}
            onStatus={(s) => setEditorReady(s === 'ready')}
            onChange={() => { dirty.current = true; setSaveState('dirty'); }}
            onSave={persist}
          />
          <div className="dio-hint">双击节点修改文字 · 拖动节点调整位置 · Ctrl+S 保存</div>
        </>
      )}
      <Modal
        open={ask === 'leave'} onClose={() => setAsk(null)} icon="alert" title="有未保存的修改" w={440}
        foot={<><Button onClick={() => setAsk(null)}>继续编辑</Button><Button kind="danger" onClick={() => leave(true)}>放弃修改</Button></>}
      >
        <div className="sm muted">返回预览会丢掉这些修改。可以先在编辑器里保存。</div>
      </Modal>
      <Modal
        open={!!ask?.conflict} onClose={() => setAsk(null)} icon="alert" title="流程图已被他人或助手修改" w={460}
        foot={(
          <>
            <Button onClick={() => setAsk(null)}>继续编辑</Button>
            <Button
              kind="primary"
              onClick={async () => {
                setAsk(null);
                try {
                  accept(await api.readFile(project.id, path));
                  dirty.current = false;
                  setSaveState('clean');
                  setMode('view');
                } catch (e) { setDoc({ error: e.message || errorText(e) }); }
              }}
            >
              载入最新版本
            </Button>
          </>
        )}
      >
        <div className="sm muted">当前编辑的内容还在。可以继续编辑，或放弃这次修改、载入最新版本。</div>
      </Modal>
    </div>
  );
}
