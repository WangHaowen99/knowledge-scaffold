import { FullscreenExitOutlined, FullscreenOutlined } from '@ant-design/icons';
import { Alert, Button, Segmented, Spin, Tooltip, message } from 'antd';
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import '@xyflow/react/dist/style.css';
import { api } from '../../lib/api.js';
import { setWorkspaceDirty } from '../../lib/workspaceDirty.js';
import FlowchartEditor from './FlowchartEditor.jsx';
import './flowchart-editor.css';
import { FlowchartValidationError, parseDrawioToDocument, serializeDocumentToDrawio } from './drawioConvert.js';
import { canRepairDrawio } from './drawioRepair.js';

// drawio 原生编辑器（iframe，重）只在用户切换或解析失败时加载
const DrawioView = lazy(() => import('../DrawioView.jsx'));
const DrawioRepairPanel = lazy(() => import('./DrawioRepairPanel.jsx'));

/* drawio 流程图视图：默认用 modeling 同款 ReactFlow 编辑器（秒开、样式统一），
   保存时按资产节点样式约定回写 drawio XML；压缩存储或解析失败回退 drawio 编辑器。 */
// 「AI 推荐新流程」交给助手：对照资料分析报告补画还没有的业务流程（ontology-flowchart Mode D）
const RECOMMEND_FLOWCHART_COMMAND = '/ontology-flowchart 补充流程图';

export default function FlowchartView({ project, path, drawioAvailable, onWorkspaceChanged, onCommand }) {
  const [mode, setMode] = useState('standard'); // standard | drawio
  const [state, setState] = useState({ loading: true });
  const revisionRef = useRef('');
  const loadGenerationRef = useRef(0);
  const editorStateRef = useRef(null);
  const drawioDirtyRef = useRef(false);
  const drawioEditingRef = useRef(false);
  const repairDirtyRef = useRef(false);
  const switchGenerationRef = useRef(0);
  const onRepairDirtyChange = useCallback((dirty) => { repairDirtyRef.current = dirty; }, []);
  const onDrawioStateChange = useCallback((saveState) => { drawioDirtyRef.current = saveState !== 'clean'; }, []);
  const onDrawioModeChange = useCallback((editing) => {
    drawioEditingRef.current = editing;
    if (editing) switchGenerationRef.current += 1;
  }, []);
  // 标准编辑器全屏：铺满视口，Esc 退出（drawio 高级编辑自带全屏按钮）
  const [fullscreen, setFullscreen] = useState(false);

  const load = useCallback(() => {
    const generation = ++loadGenerationRef.current;
    editorStateRef.current = null;
    repairDirtyRef.current = false;
    api
      .readFile(project.id, path)
      .then((data) => {
        if (generation !== loadGenerationRef.current) return;
        if (data.truncated) throw new Error('流程图超过读取大小限制，请下载文件后编辑');
        try {
          const doc = parseDrawioToDocument(data.content, path.split('/').pop());
          revisionRef.current = data.revision;
          setState({ loading: false, doc, revision: data.revision });
        } catch (error) {
          const repairable = error instanceof FlowchartValidationError
            && canRepairDrawio(data.content);
          const fallbackHint = error.message === 'unsupported wrapped cell'
            ? '此图包含链接或自定义属性，请用 drawio 高级编辑器打开'
            : error.message === 'compressed' ? '压缩格式，已回退 drawio 编辑器' : '';
          setState({ loading: false, fallback: true,
            validationError: error instanceof FlowchartValidationError ? error.message : '',
            fallbackHint,
            repairContent: repairable ? data.content : '',
            revision: data.revision });
        }
      })
      .catch((error) => {
        if (generation === loadGenerationRef.current) setState({ loading: false, error: error.message });
      });
  }, [project.id, path]);

  useEffect(() => {
    switchGenerationRef.current += 1;
    drawioDirtyRef.current = false;
    drawioEditingRef.current = false;
    setMode('standard');
    setFullscreen(false);
    setState({ loading: true });
    load();
    return () => {
      loadGenerationRef.current += 1;
      switchGenerationRef.current += 1;
    };
  }, [load]);

  useEffect(() => () => setWorkspaceDirty('flowchart-editor', false), []);

  useEffect(() => {
    if (!fullscreen) return undefined;
    const onKey = (event) => {
      if (event.key === 'Escape') setFullscreen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fullscreen]);

  useEffect(() => {
    const guard = (event) => {
      if (!editorStateRef.current?.dirty && !repairDirtyRef.current && !drawioDirtyRef.current) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, []);

  const save = useCallback(
    async (doc) => {
      const xml = serializeDocumentToDrawio(doc);
      const saved = await api.writeFile(project.id, path, xml, revisionRef.current);
      revisionRef.current = saved.revision;
      setState((prev) => ({ ...prev, revision: saved.revision }));
      message.success('流程图已保存');
      onWorkspaceChanged?.();
      return { savedRevision: saved.revision };
    },
    [project.id, path, onWorkspaceChanged],
  );

  if (state.loading) return <Spin style={{ margin: 48 }} />;
  if (state.error) return <Alert type="error" showIcon message={state.error} />;

  const useDrawio = mode === 'drawio' || (state.fallback && !state.repairContent);
  const expanded = fullscreen && !useDrawio;
  const switchEditor = async (next) => {
    if (next === 'drawio' && useDrawio) {
      switchGenerationRef.current += 1;
      return;
    }
    if (next === mode && !state.fallback) return;
    if (next === 'drawio' && (editorStateRef.current?.dirty || editorStateRef.current?.saveStatus === 'saving' || repairDirtyRef.current)
      && !window.confirm('当前流程图有未保存修改。切换到原生 drawio 编辑器会丢弃这些修改，确定继续吗？')) return;
    if (next === 'standard' && useDrawio) {
      if (drawioEditingRef.current) {
        message.warning('请先返回 drawio 预览，再切换到标准编辑器');
        return;
      }
      if (drawioDirtyRef.current) {
        message.warning('请先保存或放弃 drawio 编辑器中的修改');
        return;
      }
      const generation = ++switchGenerationRef.current;
      try {
        const data = await api.readFile(project.id, path);
        if (generation !== switchGenerationRef.current || drawioDirtyRef.current || drawioEditingRef.current) return;
        if (data.truncated) throw new Error('流程图超过读取大小限制');
        let doc;
        let repairContent = '';
        try {
          doc = parseDrawioToDocument(data.content, path.split('/').pop());
        } catch (error) {
          if (!(error instanceof FlowchartValidationError) || !canRepairDrawio(data.content)) throw error;
          repairContent = data.content;
        }
        if (generation !== switchGenerationRef.current || drawioDirtyRef.current || drawioEditingRef.current) return;
        revisionRef.current = data.revision;
        setState(repairContent
          ? { loading: false, fallback: true, repairContent, revision: data.revision }
          : { loading: false, doc, revision: data.revision });
        drawioDirtyRef.current = false;
        setMode('standard');
      } catch (error) {
        if (generation !== switchGenerationRef.current) return;
        message.info(error.message === 'unsupported wrapped cell'
          ? '此图包含链接或自定义属性，请继续使用 drawio 高级编辑器'
          : error.message);
      }
      return;
    }
    switchGenerationRef.current += 1;
    if (next === 'drawio') drawioDirtyRef.current = false;
    setMode(next);
    editorStateRef.current = null;
    setFullscreen(false);
  };
  return (
    <div className={`flowchart-host${expanded ? ' fullscreen' : ''}`}>
      <div className="flowchart-host-bar">
        <Segmented
          size="small"
          value={useDrawio ? 'drawio' : 'standard'}
          onChange={switchEditor}
          options={[
            { label: '标准编辑器', value: 'standard' },
            { label: 'drawio 高级编辑', value: 'drawio' },
          ]}
        />
        {state.fallback && state.fallbackHint && (
          <span className="flowchart-host-hint">{state.fallbackHint}</span>
        )}
        {!useDrawio && (
          <Tooltip title={expanded ? '退出全屏（Esc）' : '全屏编辑'}>
            <Button
              size="small"
              className="flowchart-host-fullscreen"
              aria-label={expanded ? '退出全屏' : '全屏编辑'}
              icon={expanded ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
              onClick={() => setFullscreen(!expanded)}
            />
          </Tooltip>
        )}
      </div>
      {state.validationError && !state.repairContent && (
        <Alert type="error" showIcon message={`标准编辑器无法打开：${state.validationError}`} />
      )}
      {state.repairContent && mode !== 'drawio' ? (
        <Suspense fallback={<Spin style={{ margin: 48 }} />}>
          <DrawioRepairPanel project={project} path={path} content={state.repairContent}
            revision={state.revision} onDirtyChange={onRepairDirtyChange}
            onSaved={() => { onWorkspaceChanged?.(); load(); }} />
        </Suspense>
      ) : useDrawio ? (
        <Suspense fallback={<Spin style={{ margin: 48 }} />}>
          <DrawioView
            project={project}
            path={path}
            editorAvailable={drawioAvailable}
            onSaved={() => {
              onWorkspaceChanged?.();
            }}
            onStateChange={onDrawioStateChange}
            onEditModeChange={onDrawioModeChange}
          />
        </Suspense>
      ) : (
        <FlowchartEditor
          key={path}
          document={state.doc}
          savedRevision={state.revision}
          onSave={save}
          onStateChange={(nextState) => {
            editorStateRef.current = nextState;
            setWorkspaceDirty(
              'flowchart-editor',
              nextState.dirty || nextState.saveStatus === 'saving',
            );
          }}
          onRecommendDiagram={onCommand ? () => onCommand(RECOMMEND_FLOWCHART_COMMAND) : undefined}
        />
      )}
    </div>
  );
}
