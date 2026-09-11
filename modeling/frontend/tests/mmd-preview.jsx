import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import '@xyflow/react/dist/style.css';
import '../src/styles.css';
import '../src/workbench/tokens.css';
import '../src/workbench/base.css';
import '../src/workbench/app.css';
import '../src/workbench/content-view.css';
import { MermaidPreview } from '../src/workbench/center/FlowView.jsx';
import { parseMmdPresentation } from '../src/workbench/center/mmdPresentation.js';
const sample = '%% 带前置注释\nflowchart TD\nstart([开始]) --> check{资料完整？}\ncheck -->|通过| prepare[生成候选方案并保留所有原始证据与完整的业务说明，长中文不能被两行省略]\ncheck -.->|不通过| retry[补充材料]\nretry --> check\nprepare ==> done([结束])';
function App() {
  const [source, setSource] = useState(sample);
  const [focus, setFocus] = useState('');
  const [second, setSecond] = useState(false);
  window.previewTest = { setSource, setFocus, setSecond, parse: parseMmdPresentation };
  return <div className="wb" style={{padding:20}}>
    <h1 style={{fontSize:20,marginBottom:16}}>MMD 流程预览</h1>
    <section className="wb-doc"><MermaidPreview source={source} path="diagrams/流程.mmd" focusNode={focus} /></section>
    {second && <section className="wb-diff-preview"><MermaidPreview source={'flowchart LR\nother[候选节点] --> finish([候选结束])'} path="diagrams/候选.mmd" sourceLabel="源码差异" /></section>}
  </div>;
}
createRoot(document.getElementById('root')).render(<App />);
