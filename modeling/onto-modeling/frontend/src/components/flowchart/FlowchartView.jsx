import { FullscreenExitOutlined, FullscreenOutlined } from '@ant-design/icons';
import { Alert, Button, Segmented, Spin, Tooltip, message } from 'antd';
import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import '@xyflow/react/dist/style.css';
import { api } from '../../api/files.js';
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
  // 标准编辑器全屏：铺满视口，Esc 退出（drawio 高级编辑自带全屏按钮）
  const [fullscreen, setFullscreen] = useState(false);

  const load = useCallback(() => {
    setState({ loading: true });
    api
      .readFile(project.id, path)
      .then((data) => {
        try {
          const doc = parseDrawioToDocument(data.content, path.split('/').pop());
          setState({ loading: false, doc, revision: data.revision });
        } catch {
          setState({ loading: false, fallback: true });
        }
      })
      .catch((error) => setState({ loading: false, error: error.message }));
  }, [project.id, path]);

  useEffect(() => {
    setMode('standard');
    setFullscreen(false);
    load();
  }, [load]);

  useEffect(() => {
    if (!fullscreen) return undefined;
    const onKey = (event) => {
      if (event.key === 'Escape') setFullscreen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fullscreen]);

  const save = useCallback(
    async (doc) => {
      const xml = serializeDocumentToDrawio(doc);
      const saved = await api.writeFile(project.id, path, xml, state.revision || '');
      setState((prev) => ({ ...prev, revision: saved.revision }));
      message.success('流程图已保存');
      onWorkspaceChanged?.();
      return { savedRevision: saved.revision };
    },
    [project.id, path, state.revision, onWorkspaceChanged],
  );

  if (state.loading) return <Spin style={{ margin: 48 }} />;
  if (state.error) return <Alert type="error" showIcon message={state.error} />;

  const useDrawio = mode === 'drawio' || state.fallback;
  const expanded = fullscreen && !useDrawio;
  return (
    <div className={`flowchart-host${expanded ? ' fullscreen' : ''}`}>
      <div className="flowchart-host-bar">
        <Segmented
          size="small"
          value={useDrawio ? 'drawio' : 'standard'}
          onChange={(next) => {
            if (state.fallback && next === 'standard') {
              message.info('该文件使用压缩存储，暂只支持 drawio 编辑器打开');
              return;
            }
            setMode(next);
            setFullscreen(false);
            if (next === 'standard') load(); // 从 drawio 切回时重读（可能已被保存）
          }}
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
          key={`${path}:${state.revision}`}
          document={state.doc}
          savedRevision={state.revision}
          onSave={save}
          onRecommendDiagram={onCommand ? () => onCommand(RECOMMEND_FLOWCHART_COMMAND) : undefined}
        />
      )}
    </div>
  );
}
