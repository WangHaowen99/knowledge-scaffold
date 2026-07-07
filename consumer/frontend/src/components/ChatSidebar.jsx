import { useState, useRef, useEffect } from 'react';
import {
  Trash2,
  MessageSquarePlus,
  Database,
  ChevronRight,
  ChevronDown,
  Loader2,
  PanelLeftOpen,
  Clock3,
} from 'lucide-react';

const SessionTitle = ({ title }) => {
  const titleRef = useRef(null);
  const [isOverflowing, setIsOverflowing] = useState(false);

  useEffect(() => {
    const checkOverflow = () => {
      if (titleRef.current) {
        const element = titleRef.current;
        const isOverflow = element.scrollWidth > element.clientWidth;
        setIsOverflowing(isOverflow);
      }
    };

    const timer = setTimeout(checkOverflow, 0);
    window.addEventListener('resize', checkOverflow);

    return () => {
      clearTimeout(timer);
      window.removeEventListener('resize', checkOverflow);
    };
  }, [title]);

  return (
    <div
      ref={titleRef}
      className="session-title"
      title={isOverflowing ? title : undefined}
    >
      {title}
    </div>
  );
};

function ChatSidebar({
  sessions,
  currentChatId,
  loadingSessions,
  isCollapsed,
  activeSessionIds = [],
  onSelectChat,
  onDeleteChat,
  onCreateChat,
  onToggleSidebar,
  onNavItemChange,
  navBadges = {},
}) {
  const [activeNavItem, setActiveNavItem] = useState('chat');
  const [expandedNavItems, setExpandedNavItems] = useState([]);
  const [activeOntologySubItem, setActiveOntologySubItem] = useState(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);

  const navItems = [
    { id: 'chat', label: '新对话', icon: MessageSquarePlus },
    {
      id: 'ontology',
      label: '本体管理',
      icon: Database,
      subItems: [
        { id: 'ontology-management', label: '实体/逻辑管理' },
        { id: 'operation-evolution', label: '运算进化', badgeKey: 'operationEvolution' },
      ]
    },
  ];

  const toggleNavItem = (itemId) => {
    if (expandedNavItems.includes(itemId)) {
      setExpandedNavItems(expandedNavItems.filter(id => id !== itemId));
    } else {
      setExpandedNavItems([...expandedNavItems, itemId]);
    }
  };

  const handleNavItemClick = (item) => {
    // 点击 nav-item 时，清除 nav-subitem 的选中状态
    setActiveOntologySubItem(null);

    if (item.subItems) {
      if (isCollapsed) {
        const firstSubItem = item.subItems[0];
        if (firstSubItem) {
          handleSubItemClick(firstSubItem.id);
        } else if (onNavItemChange) {
          onNavItemChange(item.id);
        }
        return;
      }

      // 如果有子项，切换展开状态
      toggleNavItem(item.id);
    } else {
      setActiveNavItem(item.id);
      if (onNavItemChange) {
        onNavItemChange(item.id);
      }
      if (item.id === 'chat') {
        onCreateChat();
      }
    }
  };

  const handleSubItemClick = (subItemId) => {
    setActiveOntologySubItem(subItemId);
    setActiveNavItem('ontology');
    if (onNavItemChange) {
      onNavItemChange('ontology', subItemId);
    }
  };

  const handleDeleteClick = (event, sessionId) => {
    event.stopPropagation();
    setConfirmDeleteId((prev) => (prev === sessionId ? null : sessionId));
  };

  const handleConfirmDelete = (event, sessionId) => {
    event.stopPropagation();
    setConfirmDeleteId(null);
    onDeleteChat(sessionId);
  };

  const handleCancelDelete = (event) => {
    event.stopPropagation();
    setConfirmDeleteId(null);
  };

  return (
    <>
      <aside className={`card session-panel ${isCollapsed ? 'collapsed' : ''}`}>
        <button 
          className="sidebar-edge-toggle"
          onClick={(e) => {
            e.stopPropagation();
            onToggleSidebar?.();
          }}
          title={isCollapsed ? '展开侧边栏' : '收缩侧边栏'}
          type="button"
        >
          <ChevronRight size={14} className={isCollapsed ? '' : 'rotated'} />
        </button>
        <nav className="nav-menu">
          {navItems.map((item) => {
            const Icon = item.icon;
            // "新对话"按钮不应该有持久的 active 状态，只有其他导航项才有
            const isActive = item.id !== 'chat' && activeNavItem === item.id;
            const isExpanded = expandedNavItems.includes(item.id);
            const hasSubItems = item.subItems && item.subItems.length > 0;

            return (
              <div key={item.id}>
                <div
                  className={`nav-item ${isActive ? 'active' : ''}`}
                  onClick={() => handleNavItemClick(item)}
                  title={isCollapsed ? item.label : undefined}
                >
                  <span className="nav-item-icon-wrap">
                    <Icon size={16} />
                  </span>
                  <span className="nav-item-label">{item.label}</span>
                  {hasSubItems && (
                    <span className="nav-item-chevron">
                      {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </span>
                  )}
                </div>
                {hasSubItems && isExpanded && !isCollapsed && (
                  <div className="nav-submenu">
                    {item.subItems.map((subItem) => {
                      const badgeCount = subItem.badgeKey ? Number(navBadges[subItem.badgeKey] || 0) : 0;
                      return (
                        <div
                          key={subItem.id}
                          className={`nav-subitem ${activeOntologySubItem === subItem.id ? 'active' : ''}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleSubItemClick(subItem.id);
                          }}
                        >
                          <span className="nav-subitem-label">{subItem.label}</span>
                          {badgeCount > 0 && (
                            <span className="nav-subitem-badge" title={`${badgeCount} 待审`}>
                              {badgeCount > 99 ? '99+' : badgeCount}
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </nav>

        {!isCollapsed && (
          <>
            <div className="session-list-header">
              <div className="session-list-title">
                <Clock3 size={13} />
                <span>历史对话</span>
              </div>
            </div>
            <div className="session-list">
              {loadingSessions ? (
                <div className="muted">Loading sessions...</div>
              ) : (() => {
                const filteredSessions = sessions.filter((session) => {
                  // Always show current session and sessions with active streams
                  if (session.id === currentChatId) return true;
                  if (activeSessionIds.includes(session.id)) return true;
                  // Show sessions even when title is empty
                  return true;
                });
                return filteredSessions.length === 0 ? (
                  <div className="muted-emptyhistory">暂无会话历史</div>
                ) : (
                  filteredSessions.map((session) => {
                    const isActive = activeNavItem === 'chat' && session.id === currentChatId;
                    const isStreaming = activeSessionIds.includes(session.id);
                    return (
                      <div
                        key={session.id}
                        className={`session-item ${isActive ? 'active' : ''} ${isStreaming ? 'streaming' : ''}`}
                        onClick={() => {
                          setConfirmDeleteId(null);
                          setActiveOntologySubItem(null);
                          setActiveNavItem('chat');
                          if (onNavItemChange) {
                            onNavItemChange('chat');
                          }
                          onSelectChat(session.id);
                        }}
                      >
                        <div className="session-row">
                          <SessionTitle title={session.title || '新对话'} />
                          <div className="session-actions-right">
                            {isStreaming && (
                              <span className="streaming-indicator" title="正在接收响应...">
                                <Loader2 size={14} className="spin" />
                              </span>
                            )}
                            <div className="delete-confirm-wrap">
                              <button
                                className="icon-btn"
                                onClick={(e) => handleDeleteClick(e, session.id)}
                                title="Delete chat"
                                type="button"
                              >
                                <Trash2 size={14} />
                              </button>
                              {confirmDeleteId === session.id && (
                                <div
                                  className="delete-confirm-popover"
                                  onClick={(e) => e.stopPropagation()}
                                >
                                  <div className="delete-confirm-text">删除该会话？</div>
                                  <div className="delete-confirm-actions">
                                    <button
                                      className="btn btn-secondary btn-compact"
                                      onClick={handleCancelDelete}
                                      type="button"
                                    >
                                      取消
                                    </button>
                                    <button
                                      className="btn btn-danger btn-compact"
                                      onClick={(e) => handleConfirmDelete(e, session.id)}
                                      type="button"
                                    >
                                      删除
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      </div>
                    );
                  })
                );
              })()}
            </div>
          </>
        )}

      </aside>

      {isCollapsed && (
        <button
          className="icon-btn topbar-toggle sidebar-toggle-float"
          onClick={onToggleSidebar}
          title="展开侧边栏"
          aria-label="展开侧边栏"
          type="button"
        >
          <PanelLeftOpen size={16} />
        </button>
      )}
    </>
  );
}

export default ChatSidebar;
