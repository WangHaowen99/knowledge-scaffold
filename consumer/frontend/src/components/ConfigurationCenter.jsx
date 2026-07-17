import {
  ArrowLeft,
  Database,
  FolderCog,
  KeyRound,
  Link2,
  SlidersHorizontal,
} from 'lucide-react';

import DataConnectionManagement from './DataConnectionManagement';
import McpManagement from './McpManagement';
import SystemSettings from './SystemSettings';
import WorkspaceManagement from './WorkspaceManagement';
import WorkspaceRuntimeConfig from './WorkspaceRuntimeConfig';

const CONFIG_SECTIONS = [
  {
    id: 'system',
    label: '系统配置',
  },
  {
    id: 'workspace',
    label: '工作空间',
  },
];

const WORKSPACE_SECTIONS = [
  {
    id: 'workspace',
    label: '空间信息',
    icon: FolderCog,
  },
  {
    id: 'workspace-connections',
    label: '数据连接',
    icon: Database,
  },
  {
    id: 'ontology-bindings',
    label: '本体绑定',
    icon: Link2,
  },
  {
    id: 'workspace-runtime',
    label: '运行参数',
    icon: SlidersHorizontal,
  },
  {
    id: 'mcp',
    label: 'MCP 服务',
    icon: KeyRound,
  },
];

function ConfigurationCenter({
  activeSection = 'system',
  onSectionChange,
  onBack,
  workspaces = [],
  currentWorkspaceId = '',
  onWorkspacesRefresh,
  onOntologiesRefresh,
  onWorkspaceChange,
  currentUser,
}) {
  const currentTopSection = activeSection === 'system' ? 'system' : 'workspace';
  const currentWorkspaceSection = WORKSPACE_SECTIONS.some((item) => item.id === activeSection)
    ? activeSection
    : 'workspace';
  const currentWorkspace = workspaces.find((workspace) => workspace.id === currentWorkspaceId) || null;
  const isOntologyBindingSection = currentTopSection === 'workspace' && currentWorkspaceSection === 'ontology-bindings';
  const isWorkspaceRuntimeSection = currentTopSection === 'workspace' && currentWorkspaceSection === 'workspace-runtime';
  const isMcpSection = currentTopSection === 'workspace' && currentWorkspaceSection === 'mcp';

  return (
    <div className="configuration-center-page">
      <div className="configuration-center-header">
        <div className="configuration-center-heading">
          <button
            className="system-settings-back"
            onClick={onBack}
            type="button"
            aria-label="返回对话"
            title="返回对话"
          >
            <ArrowLeft size={16} />
          </button>
          <div>
            <h2>配置中心</h2>
            <p>全局能力在系统配置中管理，空间资源统一归属当前工作空间。</p>
          </div>
        </div>
      </div>

      <div className="configuration-center-tab-strip" role="tablist" aria-label="配置中心分组">
        {CONFIG_SECTIONS.map((section) => {
          const isActive = section.id === currentTopSection;
          return (
            <button
              key={section.id}
              className={`configuration-center-tab-item ${isActive ? 'active' : ''}`}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => onSectionChange?.(section.id)}
            >
              <span>{section.label}</span>
            </button>
          );
        })}
      </div>

      <div className="configuration-center-body">
        {currentTopSection === 'system' && (
          <SystemSettings
            canEdit={currentUser?.role === 'admin'}
            embedded
          />
        )}

        {currentTopSection === 'workspace' && (
          <div className="configuration-workspace-shell">
            <div className="configuration-workspace-tab-strip" role="tablist" aria-label="工作空间配置分组">
              {WORKSPACE_SECTIONS.map((section) => {
                const isActive = section.id === currentWorkspaceSection;
                const SectionIcon = section.icon;
                return (
                  <button
                    key={section.id}
                    className={`configuration-workspace-tab-item ${isActive ? 'active' : ''}`}
                    type="button"
                    role="tab"
                    aria-selected={isActive}
                    onClick={() => onSectionChange?.(section.id)}
                  >
                    <SectionIcon className="configuration-workspace-tab-icon" size={16} strokeWidth={2} aria-hidden="true" />
                    <span>{section.label}</span>
                  </button>
                );
              })}
            </div>

            {currentWorkspaceSection === 'workspace' && (
              <WorkspaceManagement
                workspaces={workspaces}
                currentWorkspaceId={currentWorkspaceId}
                onWorkspacesRefresh={onWorkspacesRefresh}
                onOntologiesRefresh={onOntologiesRefresh}
                onWorkspaceChange={onWorkspaceChange}
                currentUser={currentUser}
                embedded
                showDataConnections={false}
              />
            )}

            {isWorkspaceRuntimeSection && (
              <WorkspaceRuntimeConfig
                currentWorkspaceId={currentWorkspaceId}
                currentWorkspaceName={currentWorkspace?.name || currentWorkspaceId}
                canEdit={currentUser?.role === 'admin'}
              />
            )}

            {isMcpSection && (
              <McpManagement
                currentWorkspaceId={currentWorkspaceId}
                currentWorkspaceName={currentWorkspace?.name || currentWorkspaceId}
                canEdit={currentUser?.role === 'admin'}
              />
            )}

            {currentWorkspaceSection === 'workspace-connections' && (
              <section className="configuration-workspace-panel">
                <div className="configuration-section-title">
                  <h3>数据连接</h3>
                  <p>管理当前工作空间可复用的业务数据连接，供本体绑定使用。点击行可查看或编辑连接详情。</p>
                </div>
                <DataConnectionManagement
                  currentWorkspaceId={currentWorkspaceId}
                  canEdit={currentUser?.role === 'admin'}
                  view="connections"
                  onOntologiesChange={onOntologiesRefresh}
                />
              </section>
            )}

            {isOntologyBindingSection && (
              <section className="configuration-ontology-binding-panel">
                <div className="configuration-section-title">
                  <h3>本体绑定</h3>
                  <p>为当前工作空间下的本体指定业务数据连接；每个本体必须明确绑定一个业务数据连接，运行时不做隐式继承。</p>
                </div>
                <DataConnectionManagement
                  currentWorkspaceId={currentWorkspaceId}
                  canEdit={currentUser?.role === 'admin'}
                  view="bindings"
                  onOntologiesChange={onOntologiesRefresh}
                />
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default ConfigurationCenter;
