import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { store, actions } from './lib/appStore.js';
import { useStore } from './lib/store.js';
import { startMockActivity } from './lib/mock/stream.js';
import { subscribeAgent } from './lib/api.js';
import { prefetchStudioBootstrap } from './studio/studio-api.js';
import { me } from '../api.js';
import SettingsModal from '../SettingsModal.jsx';
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
const MCP_SETTINGS_PARAM = 'settings';

export default function App() {
  const route = useHashRoute();
  if (route === 'create') return <div className="wb"><CreatePage /></div>;
  return <div className="wb"><Workbench /></div>;
}

function Workbench() {
  const widths = useStore(store, selWidths);
  const drawer = useStore(store, selDrawer);
  const demo = useStore(store, selDemo);
  const interaction = useStore(store, (state) => state.interaction);
  const leftRef = useRef(null);
  const rightRef = useRef(null);
  const [globalDrag, setGlobalDrag] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(() => (
    new URLSearchParams(window.location.search).get(MCP_SETTINGS_PARAM) === 'mcp'
  ));
  const [settingsUser, setSettingsUser] = useState(null);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [mobilePane, setMobilePane] = useState('assistant');

  useEffect(() => {
    if (interaction) setMobilePane('assistant');
  }, [interaction?.id]);

  const openMcpSettings = async () => {
    setSettingsLoading(true);
    try {
      const currentUser = settingsUser || (await me()).user;
      setSettingsUser(currentUser);
      const url = new URL(window.location.href);
      if (url.searchParams.get(MCP_SETTINGS_PARAM) !== 'mcp') {
        url.searchParams.set(MCP_SETTINGS_PARAM, 'mcp');
        const currentState = window.history.state && typeof window.history.state === 'object'
          ? window.history.state
          : {};
        window.history.pushState(
          { ...currentState, workbenchOverlay: 'mcp' },
          '',
          `${url.pathname}${url.search}${url.hash}`,
        );
      }
      setSettingsOpen(true);
    } catch (error) {
      store.set({ error: error instanceof Error ? error.message : 'MCP 配置加载失败' });
    } finally {
      setSettingsLoading(false);
    }
  };

  const closeMcpSettings = () => {
    setSettingsOpen(false);
    const url = new URL(window.location.href);
    if (url.searchParams.get(MCP_SETTINGS_PARAM) !== 'mcp') return;
    if (window.history.state?.workbenchOverlay === 'mcp') {
      window.history.back();
      return;
    }
    url.searchParams.delete(MCP_SETTINGS_PARAM);
    window.history.replaceState(
      window.history.state,
      '',
      `${url.pathname}${url.search}${url.hash}`,
    );
  };

  useEffect(() => {
    const syncSettingsFromHistory = () => {
      setSettingsOpen(new URLSearchParams(window.location.search).get(MCP_SETTINGS_PARAM) === 'mcp');
    };
    window.addEventListener('popstate', syncSettingsFromHistory);
    return () => window.removeEventListener('popstate', syncSettingsFromHistory);
  }, []);

  useEffect(() => {
    if (demo) return startMockActivity();
    const projectId = store.get().projectId;
    if (!projectId) return undefined;
    let stopped = false;
    let unsubscribe = () => {};
    let bootstrapTimer = 0;
    const bootstrapWorkbench = () => actions.bootstrap(projectId).then(({ agent, snapshot, agentAvailable }) => {
      const shouldStart = new URLSearchParams(window.location.search).get('start') === '1';
      if (!stopped && agentAvailable && shouldStart && !store.get().autoStartSent && !agent.run) {
        store.set({ autoStartSent: true });
        const url = new URL(window.location.href);
        url.searchParams.delete('start');
        window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
        void actions.send(`请开始为“${snapshot.project.name}”建模。先完整读取已上传资料，主动确认目标并持续推进下一步。`);
      }
      if (!stopped && agentAvailable) {
        // 事件流会携带游标自动重连；瞬时断线不属于内容读取失败。
        unsubscribe = subscribeAgent(projectId, {
          onEvent: actions.handleEvent,
        });
      }
    }).catch(() => undefined);
    if (store.get().focus.kind === 'studio') {
      void prefetchStudioBootstrap(projectId, { limit: 200 }).catch(() => undefined);
      bootstrapTimer = window.setTimeout(() => { void bootstrapWorkbench(); }, 75);
    } else {
      void bootstrapWorkbench();
    }
    return () => {
      stopped = true;
      window.clearTimeout(bootstrapTimer);
      unsubscribe();
    };
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
      <TopBar onOpenMcpSettings={() => void openMcpSettings()} settingsLoading={settingsLoading} />
      <nav className="wb-mobile-tabs" aria-label="移动端工作区">
        <button type="button" className={mobilePane === 'content' ? 'on' : ''}
          onClick={() => setMobilePane('content')}>知识内容</button>
        <button type="button" className={mobilePane === 'assistant' ? 'on' : ''}
          onClick={() => setMobilePane('assistant')}>建模助手{interaction ? <span>1</span> : null}</button>
      </nav>
      <div className={`wb-body wb-mobile-${mobilePane}`}>
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
      {settingsOpen && createPortal(
        <div className="mwr wb-settings-host"><SettingsModal
          user={settingsUser}
          projectId={store.get().projectId}
          tab="mcp"
          onClose={closeMcpSettings}
        /></div>,
        document.body,
      )}
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
