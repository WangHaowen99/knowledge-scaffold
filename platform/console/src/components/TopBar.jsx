import { useState, useRef, useEffect } from 'react';
import { Activity, Bot, Settings, ChevronDown, Check, User, LogOut, Sliders, PanelLeft, Plus, Users } from 'lucide-react';

function TopBar({
  workspaces = [],
  currentWorkspaceId = '',
  currentUser = null,
  onWorkspaceChange,
  onWorkspaceManagementClick,
  onSettingsClick,
  onConfigurationClick,
  onAccountSettingsClick,
  onUserManagementClick,
  onLocalObservabilityClick,
  onLogout,
  onToggleSidebar,
  isSidebarCollapsed = false,
  isWorkspaceManagementActive = false,
  isSettingsActive = false,
  isConfigurationActive = false,
  isAccountSettingsActive = false,
  isUserManagementActive = false,
  isLocalObservabilityActive = false,
}) {
  const [isWorkspaceOpen, setIsWorkspaceOpen] = useState(false);
  const [isUserMenuOpen, setIsUserMenuOpen] = useState(false);
  
  const workspaceRef = useRef(null);
  const userMenuRef = useRef(null);

  const currentWorkspace = workspaces.find((w) => w.id === currentWorkspaceId);
  const hasWorkspaces = workspaces.length > 0;
  const workspaceNameToDisplay = hasWorkspaces
    ? (currentWorkspace ? (currentWorkspace.name || currentWorkspace.id) : currentWorkspaceId)
    : '新建工作空间';

  useEffect(() => {
    const handleClickOutside = (event) => {
      if (workspaceRef.current && !workspaceRef.current.contains(event.target)) {
        setIsWorkspaceOpen(false);
      }
      if (userMenuRef.current && !userMenuRef.current.contains(event.target)) {
        setIsUserMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  return (
    <header className="global-topbar">
      <div className="global-topbar-left">
        <button
          className="global-sidebar-toggle"
          onClick={onToggleSidebar}
          type="button"
          title={isSidebarCollapsed ? '打开菜单' : '收起菜单'}
          aria-label={isSidebarCollapsed ? '打开菜单' : '收起菜单'}
          aria-expanded={!isSidebarCollapsed}
        >
          <PanelLeft size={18} />
        </button>

        <div className="global-topbar-brand">
          <div className="global-topbar-icon">
            <Bot size={18} />
          </div>
          <span className="global-topbar-name">本体消费智能体</span>
        </div>

        <div className="global-topbar-divider"></div>

        <div className="global-topbar-workspace-dropdown-container" ref={workspaceRef}>
          {hasWorkspaces ? (
            <div
              className={`global-topbar-workspace-picker ${isWorkspaceOpen ? 'is-open' : ''}`}
              onClick={() => setIsWorkspaceOpen(!isWorkspaceOpen)}
            >
              <div
                className={`global-topbar-workspace-icon-wrap ${isWorkspaceManagementActive ? 'active' : ''}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onWorkspaceManagementClick?.();
                  setIsWorkspaceOpen(false);
                }}
                title="管理当前工作空间"
              >
                <Settings size={14} />
              </div>
              <div className="global-topbar-workspace-main">
                <span className="global-topbar-workspace-label">当前空间</span>
                <div className="global-topbar-workspace-value">
                  <span className="global-topbar-workspace-name">{workspaceNameToDisplay}</span>
                </div>
                <span className="global-topbar-workspace-chevron">
                  <ChevronDown size={14} />
                </span>
              </div>
            </div>
          ) : (
            <div
              className="global-topbar-workspace-picker workspace-setup-btn"
              onClick={() => onWorkspaceManagementClick?.()}
            >
              <div className="global-topbar-workspace-icon-wrap workspace-setup-icon">
                <Plus size={14} />
              </div>
              <div className="global-topbar-workspace-main">
                <span className="global-topbar-workspace-name">新建工作空间</span>
              </div>
            </div>
          )}

          {isWorkspaceOpen && hasWorkspaces && (
            <div className="global-topbar-dropdown-menu workspace-menu">
              <div className="global-topbar-dropdown-inner">
                {workspaces.map((workspace) => (
                  <div
                    key={workspace.id}
                    className={`global-topbar-workspace-item ${workspace.id === currentWorkspaceId ? 'active' : ''}`}
                    onClick={() => {
                      onWorkspaceChange?.(workspace.id);
                      setIsWorkspaceOpen(false);
                    }}
                  >
                    <div className="global-topbar-workspace-item-content">
                      <span className="global-topbar-workspace-item-title">{workspace.name || workspace.id}</span>
                      <span className="global-topbar-workspace-item-subtitle">{workspace.id}</span>
                    </div>
                    <div className="global-topbar-workspace-item-check">
                      {workspace.id === currentWorkspaceId && <Check size={16} strokeWidth={3} />}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="global-topbar-right">
        <button
          className={`global-config-button ${isConfigurationActive ? 'active' : ''}`}
          type="button"
          onClick={() => onConfigurationClick?.('system')}
          title="配置中心"
        >
          <Sliders size={15} />
          <span>配置中心</span>
        </button>
        <div className="global-topbar-user-container" ref={userMenuRef}>
          <div 
            className={`global-topbar-user-profile ${isUserMenuOpen ? 'active' : ''}`}
            onClick={() => setIsUserMenuOpen(!isUserMenuOpen)}
          >
            <div className="global-topbar-user-avatar">
              <User size={16} />
            </div>
            <div className="global-topbar-user-details">
              <span className="global-topbar-user-name">{currentUser?.username || '用户'}</span>
              <span className="global-topbar-user-role">{currentUser?.role === 'admin' ? '管理员' : '普通用户'}</span>
            </div>
            <ChevronDown size={14} className={`global-topbar-user-chevron ${isUserMenuOpen ? 'rotated' : ''}`} />
          </div>

          {isUserMenuOpen && (
            <div className="global-topbar-dropdown-menu user-menu">
              <div className="global-topbar-dropdown-inner">
                <div className="user-menu-header">
                  <p className="user-menu-greeting">您好, {currentUser?.username}</p>
                  <p className="user-menu-email">{currentUser?.role === 'admin' ? '系统管理员权限' : '标准用户权限'}</p>
                </div>
                
                <div className="global-topbar-dropdown-divider"></div>

                <button
                  className={`user-menu-item ${isAccountSettingsActive ? 'active' : ''}`}
                  onClick={() => {
                    onAccountSettingsClick?.();
                    setIsUserMenuOpen(false);
                  }}
                >
                  <User size={15} />
                  <span>账号设置</span>
                </button>

                {currentUser?.role === 'admin' && (
                  <>
                    <button
                      className={`user-menu-item ${isUserManagementActive ? 'active' : ''}`}
                      onClick={() => {
                        onUserManagementClick?.();
                        setIsUserMenuOpen(false);
                      }}
                    >
                      <Users size={15} />
                      <span>用户管理</span>
                    </button>
                    <button
                      className={`user-menu-item ${isLocalObservabilityActive ? 'active' : ''}`}
                      onClick={() => {
                        onLocalObservabilityClick?.();
                        setIsUserMenuOpen(false);
                      }}
                    >
                      <Activity size={15} />
                      <span>本地观测分析</span>
                    </button>
                  </>
                )}

                <button
                  className={`user-menu-item ${isSettingsActive || isConfigurationActive ? 'active' : ''}`}
                  onClick={() => {
                    if (onConfigurationClick) {
                      onConfigurationClick('system');
                    } else {
                      onSettingsClick?.();
                    }
                    setIsUserMenuOpen(false);
                  }}
                >
                  <Sliders size={15} />
                  <span>配置中心</span>
                </button>

                <div className="global-topbar-dropdown-divider"></div>
                
                <button 
                  className="user-menu-item logout"
                  onClick={() => {
                    onLogout?.();
                    setIsUserMenuOpen(false);
                  }}
                >
                  <LogOut size={15} />
                  <span>退出登录</span>
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}

export default TopBar;
