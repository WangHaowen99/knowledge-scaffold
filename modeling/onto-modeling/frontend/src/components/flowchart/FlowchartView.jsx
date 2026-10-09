import { Alert, Button, Segmented, Spin, message } from 'antd';
import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import '@xyflow/react/dist/style.css';
import { api } from '../../lib/api.js';
import FlowchartEditor from './FlowchartEditor.jsx';
import './flowchart-editor.css';
import { parseDrawioToDocument, serializeDocumentToDrawio } from './drawioConvert.js';

// drawio 原生编辑器（iframe，重）只在用户切换或解析失败时加载
const DrawioView = lazy(() => import('../DrawioView.jsx'));

/* drawio 流程图视图：默认用 modeling 同款 ReactFlow 编辑器（秒开、样式统一），
   保存时按资产节点样式约定回写 drawio XML；压缩存储或解析失败回退 drawio 编辑器。 */
export default function FlowchartView({ project, path, drawioAvailable, onWorkspaceChanged }) {
  const [mode, setMode] = useState('standard'); // standard | drawio
  const [state, setState] = useState({ loading: true });

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
    load();
  }, [load]);

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
  return (
    <div className="flowchart-host">
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
        />
      )}
    </div>
  );
}
