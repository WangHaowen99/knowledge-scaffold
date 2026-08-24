import { useEffect, useRef, useState } from 'react';
import { store, actions } from './lib/appStore.js';
import { useStore } from './lib/store.js';
import { startMockActivity } from './lib/mock/stream.js';
import { subscribeAgent } from './lib/api.js';
import TopBar from './shell/TopBar.jsx';
import Grip from './shell/Grip.jsx';
import LeftPane from './left/LeftPane.jsx';
import CenterPane from './center/CenterPane.jsx';
import RightPane from './right/RightPane.jsx';
import ProcessDrawer from './shell/ProcessDrawer.jsx';
import CreatePage from './pages/CreatePage.jsx';

const selWidths = (s) => s.widths;
const selDrawer = (s) => s.drawerOpen;
const selDemo = (s) => s.demo;

export default function App() {
  const route = useHashRoute();
  if (route === 'create') return <div className="wb"><CreatePage /></div>;
  return <div className="wb"><Workbench /></div>;
}

function Workbench() {
  const widths = useStore(store, selWidths);
  const drawer = useStore(store, selDrawer);
  const demo = useStore(store, selDemo);
  const leftRef = useRef(null);
  const rightRef = useRef(null);
  const [globalDrag, setGlobalDrag] = useState(false);

  useEffect(() => {
    if (demo) return startMockActivity();
    const projectId = store.get().projectId;
    if (!projectId) return undefined;
    let stopped = false;
    let unsubscribe = () => {};
    void actions.bootstrap(projectId).then(({ agent, snapshot }) => {
      const shouldStart = new URLSearchParams(window.location.search).get('start') === '1';
      if (!stopped && shouldStart && !store.get().autoStartSent && !agent.run) {
        store.set({ autoStartSent: true });
        const url = new URL(window.location.href);
        url.searchParams.delete('start');
        window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
        void actions.send(`请开始为“${snapshot.project.name}”建模。先完整读取已上传资料，主动确认目标并持续推进下一步。`);
      }
      if (!stopped) {
        unsubscribe = subscribeAgent(projectId, {
          onEvent: actions.handleEvent,
          onError: (error) => store.set({ error: error instanceof Error ? error.message : '助手连接中断，正在重连' }),
        });
      }
    }).catch(() => undefined);
    return () => { stopped = true; unsubscribe(); };
  }, [demo]);

  return (
    <div className={`wb-app${globalDrag ? ' wb-global-drag' : ''}`}
      onDragEnter={(event) => { if (event.dataTransfer?.types?.includes('Files')) setGlobalDrag(true); }}
      onDragOver={(event) => { if (event.dataTransfer?.types?.includes('Files')) event.preventDefault(); }}
      onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setGlobalDrag(false); }}
      onDrop={(event) => {
        if (!event.dataTransfer?.files?.length) return;
        event.preventDefault(); setGlobalDrag(false); void actions.upload(event.dataTransfer.files);
      }}>
      <TopBar />
      <div className="wb-body">
        <aside className="wb-pane wb-pane-l" ref={leftRef} style={{ width: widths.left }}>
          <LeftPane />
        </aside>
        <Grip side="left" paneRef={leftRef} />

        <main className="wb-pane wb-pane-c">
          <CenterPane />
        </main>

        <Grip side="right" paneRef={rightRef} />
        <aside className="wb-pane wb-pane-r" ref={rightRef} style={{ width: widths.right }}>
          <RightPane />
        </aside>
      </div>
      {drawer && <ProcessDrawer onClose={actions.toggleDrawer} />}
      {globalDrag && <div className="wb-drop-overlay"><b>松手即上传并解析</b><span>你可以继续对话，解析结果会回到消息流</span></div>}
    </div>
  );
}

/** 极简 hash 路由：只有两个页面，不值得引 react-router。 */
function useHashRoute() {
  const [hash, setHash] = useState(() => window.location.hash.replace(/^#\/?/, ''));
  useEffect(() => {
    const on = () => setHash(window.location.hash.replace(/^#\/?/, ''));
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return hash || 'workbench';
}
