import { FullscreenExitOutlined, FullscreenOutlined } from '@ant-design/icons';
import { Alert, Button, Segmented, Spin, Tooltip, message } from 'antd';
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import '@xyflow/react/dist/style.css';
import { api } from '../../lib/api.js';
import { setWorkspaceDirty } from '../../lib/workspaceDirty.js';
import FlowchartEditor from './FlowchartEditor.jsx';
import './flowchart-editor.css';
import { parseDrawioToDocument, serializeDocumentToDrawio } from './drawioConvert.js';

// drawio 原生编辑器（iframe，重）只在用户切换或解析失败时加载
const DrawioView = lazy(() => import('../DrawioView.jsx'));

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
  // 标准编辑器全屏：铺满视口，Esc 退出（drawio 高级编辑自带全屏按钮）
  const [fullscreen, setFullscreen] = useState(false);

  const load = useCallback(() => {
    const generation = ++loadGenerationRef.current;
    editorStateRef.current = null;
    api
      .readFile(project.id, path)
      .then((data) => {
        if (generation !== loadGenerationRef.current) return;
        if (data.truncated) throw new Error('流程图超过读取大小限制，请下载文件后编辑');
        try {
          const doc = parseDrawioToDocument(data.content, path.split('/').pop());
          revisionRef.current = data.revision;
          setState({ loading: false, doc, revision: data.revision });
        } catch {
          setState({ loading: false, fallback: true });
        }
      })
      .catch((error) => {
        if (generation === loadGenerationRef.current) setState({ loading: false, error: error.message });
      });
  }, [project.id, path]);

  useEffect(() => {
    setMode('standard');
    setFullscreen(false);
    setState({ loading: true });
    load();
    return () => { loadGenerationRef.current += 1; };
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
      if (!editorStateRef.current?.dirty) return;
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

  const useDrawio = mode === 'drawio' || state.fallback;
  const expanded = fullscreen && !useDrawio;
  const switchEditor = (next) => {
    if (next === mode && !state.fallback) return;
    if (next === 'drawio' && (editorStateRef.current?.dirty || editorStateRef.current?.saveStatus === 'saving')
      && !window.confirm('当前流程图有未保存修改。切换到原生 drawio 编辑器会丢弃这些修改，确定继续吗？')) return;
    if (state.fallback && next === 'standard') {
      message.info('该文件使用压缩存储，暂只支持 drawio 编辑器打开');
      return;
    }
    setMode(next);
    editorStateRef.current = null;
    setWorkspaceDirty('flowchart-editor', false);
    setFullscreen(false);
    if (next === 'standard') {
      setState({ loading: true });
      load();
    }
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
        {state.fallback && (
          <span className="flowchart-host-hint">压缩格式，已回退 drawio 编辑器</span>
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
      {useDrawio ? (
        <Suspense fallback={<Spin style={{ margin: 48 }} />}>
          <DrawioView
            project={project}
            path={path}
            editorAvailable={drawioAvailable}
            onSaved={() => {
              onWorkspaceChanged?.();
            }}
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
